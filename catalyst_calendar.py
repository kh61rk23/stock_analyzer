# ==============================================================
# catalyst_calendar.py  v1.0  (Wave 3 - FE-7)
# カタリストカレンダー・proximity_score 計算エンジン
# ==============================================================
# 【役割】
#   4大カタリスト（決算発表・中央銀行会合・経済指標・政策イベント）の
#   予定日を管理し、各銘柄/セクターへの proximity_score を算出する。
#   3カタリスト以上が14日以内に重複した場合に LINE最優先通知（トリガー㉔）。
#
# 【4大カタリスト定義】
#   Type 1: earnings      - 決算発表日（yfinance calendar から取得）
#   Type 2: central_bank  - 中央銀行会合（Fed FOMC / BOJ 定例会合）
#   Type 3: economic_data - 主要経済指標（CPI / GDP / 短観 / PMI 等）
#   Type 4: policy_event  - 政策イベント（NEDO発表 / 規制変更 / 議会採決）
#
# 【proximity_score 計算式】
#   CATALYST_PROXIMITY_DAYS   = [14, 30, 60]
#   CATALYST_PROXIMITY_SCORES = [1.0, 0.6, 0.3]
#   $proximity\_score = SCORES[i]$ if $days\_until \leq DAYS[i]$
#
# 【catalyst_bonus（screener加算）】
#   $catalyst\_bonus = proximity\_score \times CATALYST\_BONUS\_MAX(5.0)$
#   複数カタリストが重複する場合は最高スコアを採用（加算ではない）
#
# 【3点重複（Triple Catalyst）発動条件】
#   14日以内に3種類以上のカタリストが同一ティッカー/セクターに集中
#   → LINE最優先通知（alert_notifier.py トリガー㉔）
#
# 【生成キャッシュ】
#   catalyst_calendar.json   TTL: 前日生成分まで（FOMC 元リストが変われば即破棄）
#   catalyst_history.json    TTL: 365日
#
# 【呼び出しパターン（stock_analyzer_main.py WEEKLY_MODULES から）】
#   from catalyst_calendar import run_catalyst_calendar, calc_catalyst_bonus
#   cat_result = run_catalyst_calendar(cfg, results=results)
#   results["catalyst"] = cat_result
#
# 【依存】
#   yfinance（決算日取得）、標準ライブラリのみ
#   ★ 全関数を try/except で保護。失敗時は bonus=0.0 / empty結果を返す。
# ==============================================================

from __future__ import annotations

import json
import time
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

# ==============================================================
# BLOCK 0: 定数・マスタカレンダー
# ==============================================================

_PROXIMITY_DAYS_DEFAULT   = [14, 30, 60]
_PROXIMITY_SCORES_DEFAULT = [1.0, 0.6, 0.3]

from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'CC_CATALYST_BONUS_MAX': '_CATALYST_BONUS_MAX',
    'CC_TRIPLE_THRESHOLD': '_TRIPLE_THRESHOLD',
    'B4_CATALYST_CALENDAR_HISTORY_TTL_DAYS': '_HISTORY_TTL_DAYS',
    'B4_CATALYST_CALENDAR_TRIPLE_WINDOW_DAYS': '_TRIPLE_WINDOW_DAYS',
    'B5_CATALYST_CALENDAR_FETCH_DELAY': '_FETCH_DELAY',
}

_CATALYST_BONUS_MAX       = 5.0
_HISTORY_TTL_DAYS         = 365
_FETCH_DELAY              = 0.5
_TRIPLE_THRESHOLD         = 3     # 3種類以上でTriple Catalyst発動
_TRIPLE_WINDOW_DAYS       = 14    # Triple判定ウィンドウ（日）

# 2026年 Fed FOMC 日程（議決日）
# 2026-09-14: 本リストと fed_calendar_fetcher.FOMC_MEETINGS_2026 が別々に
# 手書きされており、8回中4回（7月・9月・10/11月・12月）が食い違っていた。
# 同じ 9/14 のメールで日次は「FOMC 1日後」、週次は「2026-09-17（3日後）」と
# 矛盾して出ていたのはこのため。fed_calendar_fetcher を正本とし
# （会合初日＋1日＝議決日）、取り込めない環境だけ下の旧リストに落とす。
_FED_FOMC_2026_FALLBACK = [
    "2026-01-29", "2026-03-19", "2026-05-07",
    "2026-06-18", "2026-07-30", "2026-09-17",
    "2026-10-29", "2026-12-17",
]


def _fomc_decision_dates_2026() -> list:
    """fed_calendar_fetcher の会合初日から議決日（翌日）を組み立てる。"""
    try:
        from fed_calendar_fetcher import FOMC_MEETINGS_2026 as _starts
        from datetime import datetime as _dt, timedelta as _td
        out = [(_dt.strptime(d, "%Y-%m-%d") + _td(days=1)).strftime("%Y-%m-%d")
               for d in _starts]
        return out or list(_FED_FOMC_2026_FALLBACK)
    except Exception:
        return list(_FED_FOMC_2026_FALLBACK)


_FED_FOMC_2026 = _fomc_decision_dates_2026()

# 2026年 BOJ 政策決定会合（発表日）
_BOJ_MEETINGS_2026 = [
    "2026-01-24", "2026-03-19", "2026-04-30",
    "2026-06-17", "2026-07-31", "2026-09-19",
    "2026-10-30", "2026-12-19",
]

# 2026年 米 CPI 発表日（概算・前年+1年シフト）
_US_CPI_2026 = [
    "2026-01-15", "2026-02-12", "2026-03-12",
    "2026-04-10", "2026-05-13", "2026-06-11",
    "2026-07-15", "2026-08-13", "2026-09-11",
    "2026-10-15", "2026-11-12", "2026-12-11",
]

# 2026年 米 GDP 発表日（速報値）
_US_GDP_2026 = [
    "2026-01-29", "2026-04-29", "2026-07-30", "2026-10-29",
]

# 2026年 日銀短観 発表日（四半期）
_BOJ_TANKAN_2026 = [
    "2026-04-02", "2026-07-02", "2026-10-01", "2027-01-05",
]

# 2026年 JP/US PMI 発表日（毎月1営業日目 概算）
_PMI_2026 = [
    "2026-01-05", "2026-02-02", "2026-03-02",
    "2026-04-01", "2026-05-04", "2026-06-01",
    "2026-07-01", "2026-08-03", "2026-09-01",
    "2026-10-01", "2026-11-02", "2026-12-01",
]

# カタリストタイプ定義
_CATALYST_TYPES = {
    "earnings":      "決算発表",
    "central_bank":  "中央銀行会合",
    "economic_data": "経済指標",
    "policy_event":  "政策イベント",
}

# セクター × 経済指標の関連マッピング
_SECTOR_ECONOMIC_AFFINITY: dict[str, list[str]] = {
    "1028": ["central_bank"],   # 銀行 → 金利感応
    "1030": ["central_bank"],   # 保険 → 金利感応
    "1020": ["central_bank"],   # 電力・ガス → 金利感応
    "1009": ["economic_data"],  # 石油・石炭 → CPI感応
    "1012": ["economic_data"],  # 鉄鋼 → GDP感応
    "1013": ["economic_data"],  # 非鉄 → CPI/GDP感応
    "1017": ["economic_data"],  # 輸送用機器 → GDP感応
    "1032": ["central_bank", "economic_data"],  # 不動産 → 金利+GDP
    "1026": ["economic_data"],  # 卸売（商社） → CPI/資源
}

# ==============================================================
# BLOCK 1: キャッシュ I/O
# ==============================================================

def _cache_dir(cfg=None) -> Path:
    try:
        return Path(getattr(cfg, "CACHE_DIR", "/tmp"))
    except Exception:
        return Path("/tmp")


def _load_calendar_cache(cfg=None) -> dict:
    """catalyst_calendar.json を読み込む。前日より古い、または FOMC 元リストが変わった場合は空 dict。"""
    try:
        p = _cache_dir(cfg) / "catalyst_calendar.json"
        if not p.exists():
            return {}
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
        gen = raw.get("generated", "0000-00-00")
        cutoff = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        if gen < cutoff:
            return {}
        # 2026-09-14: FOMC 日程の元リストが変わったら、期限内でも捨てる。
        # 同日に日程を FRB 公式へ是正したが、当日生成のキャッシュ（旧日付
        # 2026-09-17 入り）がリポジトリにコミット済みで、翌 9/15 の実行が
        # それを再利用すると FOMC 当日にも誤った日付が出続けるところだった。
        if raw.get("fomc_source") != list(_FED_FOMC_2026):
            return {}
        return raw
    except Exception:
        return {}


def _save_calendar_cache(data: dict, cfg=None) -> None:
    """catalyst_calendar.json を保存する。"""
    try:
        p = _cache_dir(cfg) / "catalyst_calendar.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps({**data, "generated": datetime.now().strftime("%Y-%m-%d"),
                        "fomc_source": list(_FED_FOMC_2026)},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def _load_catalyst_history(cfg=None) -> list:
    """catalyst_history.json を読み込む。365日以内のみ返す。"""
    try:
        p = _cache_dir(cfg) / "catalyst_history.json"
        if not p.exists():
            return []
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            return []
        cutoff = (datetime.now() - timedelta(days=_HISTORY_TTL_DAYS)
                  ).strftime("%Y-%m-%d")
        return [e for e in raw if isinstance(e, dict)
                and e.get("date", "") >= cutoff]
    except Exception:
        return []


def _save_catalyst_history(history: list, cfg=None) -> None:
    """catalyst_history.json を保存する。"""
    try:
        p = _cache_dir(cfg) / "catalyst_history.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(history, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


# ==============================================================
# BLOCK 2: グローバルカレンダー構築
# ==============================================================

def _days_until(target_date_str: str) -> int:
    """今日から target_date_str（YYYY-MM-DD）までの日数を返す。過去日は負値。"""
    try:
        target = datetime.strptime(target_date_str[:10], "%Y-%m-%d").date()
        today  = datetime.now().date()
        return (target - today).days
    except Exception:
        return 9999


def build_global_calendar(cfg=None) -> list[dict]:
    """
    中央銀行会合・経済指標の予定をグローバルカレンダーとして返す。

    config.py の FED_FOMC_DATES / BOJ_DATES などが設定されている場合はそちらを優先。

    Returns:
        [
          {
            "date":        "YYYY-MM-DD",
            "type":        "central_bank" | "economic_data",
            "subtype":     "fed_fomc" | "boj" | "us_cpi" | "us_gdp" | etc,
            "description": str,
            "market":      "US" | "JP" | "GLOBAL",
            "days_until":  int,
          }, ...
        ]
        days_until 昇順でソート済み
    """
    try:
        events: list[dict] = []

        # ── Fed FOMC ─────────────────────────────────────────
        fomc_dates = list(getattr(cfg, "FED_FOMC_DATES", _FED_FOMC_2026))
        for d in fomc_dates:
            du = _days_until(d)
            if -30 <= du <= 180:   # 過去30日〜未来180日
                events.append({
                    "date":        d,
                    "type":        "central_bank",
                    "subtype":     "fed_fomc",
                    "description": "Fed FOMC 金利決定会合",
                    "market":      "US",
                    "days_until":  du,
                })

        # ── BOJ 政策決定会合 ─────────────────────────────────
        boj_dates = list(getattr(cfg, "BOJ_MEETING_DATES", _BOJ_MEETINGS_2026))
        for d in boj_dates:
            du = _days_until(d)
            if -30 <= du <= 180:
                events.append({
                    "date":        d,
                    "type":        "central_bank",
                    "subtype":     "boj",
                    "description": "BOJ 金融政策決定会合",
                    "market":      "JP",
                    "days_until":  du,
                })

        # ── 米 CPI ───────────────────────────────────────────
        cpi_dates = list(getattr(cfg, "US_CPI_DATES", _US_CPI_2026))
        for d in cpi_dates:
            du = _days_until(d)
            if -7 <= du <= 90:
                events.append({
                    "date":        d,
                    "type":        "economic_data",
                    "subtype":     "us_cpi",
                    "description": "米 CPI（消費者物価指数）発表",
                    "market":      "US",
                    "days_until":  du,
                })

        # ── 米 GDP ───────────────────────────────────────────
        gdp_dates = list(getattr(cfg, "US_GDP_DATES", _US_GDP_2026))
        for d in gdp_dates:
            du = _days_until(d)
            if -7 <= du <= 120:
                events.append({
                    "date":        d,
                    "type":        "economic_data",
                    "subtype":     "us_gdp",
                    "description": "米 GDP 速報値発表",
                    "market":      "US",
                    "days_until":  du,
                })

        # ── 日銀短観 ─────────────────────────────────────────
        tankan_dates = list(getattr(cfg, "BOJ_TANKAN_DATES", _BOJ_TANKAN_2026))
        for d in tankan_dates:
            du = _days_until(d)
            if -7 <= du <= 120:
                events.append({
                    "date":        d,
                    "type":        "economic_data",
                    "subtype":     "boj_tankan",
                    "description": "日銀短観（全国企業短期経済観測調査）",
                    "market":      "JP",
                    "days_until":  du,
                })

        # ── PMI ──────────────────────────────────────────────
        pmi_dates = list(getattr(cfg, "PMI_DATES", _PMI_2026))
        for d in pmi_dates:
            du = _days_until(d)
            if -7 <= du <= 60:
                events.append({
                    "date":        d,
                    "type":        "economic_data",
                    "subtype":     "pmi",
                    "description": "JP/US PMI（購買担当者景気指数）",
                    "market":      "GLOBAL",
                    "days_until":  du,
                })

        return sorted(events, key=lambda x: x["days_until"])

    except Exception:
        return []


# ==============================================================
# BLOCK 3: 決算日取得（yfinance）
# ==============================================================

def fetch_earnings_dates(
    tickers: list[str],
    cfg=None,
) -> dict[str, Optional[str]]:
    """
    yfinance の calendar から各ティッカーの次回決算日を取得する。

    Returns:
        {ticker: "YYYY-MM-DD" | None}
    """
    result: dict[str, Optional[str]] = {}

    for ticker in tickers:
        try:
            import yf_cache as yf
            time.sleep(_FETCH_DELAY)
            t   = yf.Ticker(ticker)
            cal = t.calendar

            earnings_dt = None
            if cal is not None:
                # calendar は dict または DataFrame
                if isinstance(cal, dict):
                    ed = cal.get("Earnings Date")
                    if ed:
                        if isinstance(ed, (list, tuple)) and len(ed) > 0:
                            earnings_dt = str(ed[0])[:10]
                        else:
                            earnings_dt = str(ed)[:10]
                else:
                    # DataFrame の場合
                    try:
                        ed = cal.loc["Earnings Date"]
                        if hasattr(ed, "iloc"):
                            earnings_dt = str(ed.iloc[0])[:10]
                        else:
                            earnings_dt = str(ed)[:10]
                    except Exception:
                        pass

            result[ticker] = earnings_dt

        except Exception:
            result[ticker] = None

    return result


# ==============================================================
# BLOCK 4: proximity_score 計算
# ==============================================================

def calc_proximity_score(days_until: int, cfg=None) -> float:
    """
    カタリストまでの日数から proximity_score を返す。

    Args:
        days_until: カタリスト予定日までの日数（負値は過去）
        cfg:        config モジュール（None 可）

    Returns:
        float: 0.0〜1.0
        ※ 過去のイベント (days_until < 0) は 0.0
    """
    try:
        if days_until < 0:
            return 0.0

        prox_days   = list(getattr(cfg, "CATALYST_PROXIMITY_DAYS",
                                    _PROXIMITY_DAYS_DEFAULT))
        prox_scores = list(getattr(cfg, "CATALYST_PROXIMITY_SCORES",
                                    _PROXIMITY_SCORES_DEFAULT))

        for max_days, score in zip(prox_days, prox_scores):
            if days_until <= max_days:
                return float(score)

        return 0.0

    except Exception:
        return 0.0


def calc_catalyst_bonus(
    ticker:          str,
    sector_code:     str,
    catalyst_data:   dict,
    cfg=None,
) -> float:
    """
    screener 用 catalyst_bonus を算出する（最大 +5pt）。

    stock_screener.py / us_stock_screener.py の `_calc_catalyst_bonus()` 内で呼ぶ。

    $catalyst\\_bonus = max(proximity\\_score) \times CATALYST\\_BONUS\\_MAX$

    複数カタリストが重複する場合は「最高 proximity_score」を採用する（加算でない）。
    Triple Catalyst 時は +1pt ボーナスを追加。

    Args:
        ticker:        "6758.T" 形式
        sector_code:   "1016" 形式
        catalyst_data: run_catalyst_calendar() の返り値辞書
        cfg:           config モジュール（None 可）

    Returns:
        float: 0.0〜5.0（+1pt Triple ボーナスで最大6.0だがクランプで5.0）
    """
    try:
        bonus_max = float(getattr(cfg, "CATALYST_BONUS_MAX", _CATALYST_BONUS_MAX))

        # ticker_events から当該銘柄のカタリストを取得
        ticker_events = catalyst_data.get("ticker_events", {})
        events        = ticker_events.get(ticker, [])

        # セクター関連グローバルイベントも追加
        global_events = catalyst_data.get("global_events", [])
        sector_affinity = _SECTOR_ECONOMIC_AFFINITY.get(str(sector_code), [])
        sector_events = [
            e for e in global_events
            if e.get("type") in sector_affinity
            and e.get("days_until", 9999) >= 0
        ]

        all_events = list(events) + sector_events
        if not all_events:
            return 0.0

        # 最高 proximity_score を取得
        max_prox = max(
            (e.get("proximity_score", 0.0) for e in all_events),
            default=0.0,
        )

        # Triple Catalyst チェック
        triple_bonus = 0.0
        types_within_14 = {
            e.get("type", "") for e in all_events
            if e.get("days_until", 9999) <= _TRIPLE_WINDOW_DAYS
            and e.get("days_until", -1) >= 0
        }
        if len(types_within_14) >= _TRIPLE_THRESHOLD:
            triple_bonus = 1.0

        raw_bonus = max_prox * bonus_max + triple_bonus
        return round(min(bonus_max, max(0.0, raw_bonus)), 2)

    except Exception:
        return 0.0


# ==============================================================
# BLOCK 5: Triple Catalyst 検出
# ==============================================================

def detect_triple_catalyst_tickers(
    catalyst_data: dict,
    window_days:   int = _TRIPLE_WINDOW_DAYS,
) -> list[dict]:
    """
    Triple Catalyst（3種以上のカタリストが window_days 以内に重複）の
    銘柄リストを返す。

    alert_notifier.py のトリガー㉔ / weekly_report_generator.py から参照。

    Returns:
        [
          {
            "ticker":       str,
            "types":        list[str],   # 重複カタリストタイプ
            "min_days":     int,         # 最も近いカタリストまでの日数
            "max_prox":     float,       # 最高 proximity_score
            "catalyst_count": int,
          }, ...
        ]
        catalyst_count 降順にソート済み
    """
    try:
        ticker_events = catalyst_data.get("ticker_events", {})
        triples: list[dict] = []

        for ticker, events in ticker_events.items():
            types_in_window = [
                e.get("type", "")
                for e in events
                if 0 <= e.get("days_until", 9999) <= window_days
            ]
            unique_types = list(set(types_in_window))
            if len(unique_types) >= _TRIPLE_THRESHOLD:
                min_days = min(
                    (e.get("days_until", 9999) for e in events
                     if 0 <= e.get("days_until", 9999) <= window_days),
                    default=9999,
                )
                max_prox = max(
                    (e.get("proximity_score", 0.0) for e in events),
                    default=0.0,
                )
                triples.append({
                    "ticker":          ticker,
                    "types":           unique_types,
                    "min_days":        min_days,
                    "max_prox":        max_prox,
                    "catalyst_count":  len(unique_types),
                })

        return sorted(triples,
                      key=lambda x: (-x["catalyst_count"], x["min_days"]))
    except Exception:
        return []


def detect_triple_catalyst_sectors(
    catalyst_data:   dict,
    candidates:      list[dict],
    window_days:     int = _TRIPLE_WINDOW_DAYS,
    cfg=None,
) -> list[dict]:
    """
    セクターレベルの Triple Catalyst を検出する。

    セクター内の複数銘柄 + グローバルカレンダーを合算して判定。

    Returns:
        [{"sector_code": str, "types": list, "catalyst_count": int, ...}, ...]
    """
    try:
        global_events  = catalyst_data.get("global_events", [])
        sector_events: dict[str, set] = {}

        # グローバルイベントをセクターに紐付け
        for ev in global_events:
            if ev.get("days_until", 9999) < 0:
                continue
            if ev.get("days_until", 9999) > window_days:
                continue
            ev_type = ev.get("type", "")
            for sc, affinity in _SECTOR_ECONOMIC_AFFINITY.items():
                if ev_type in affinity:
                    sector_events.setdefault(sc, set()).add(ev_type)

        # screener candidates の決算情報を追加
        ticker_events = catalyst_data.get("ticker_events", {})
        for c in candidates:
            sc     = str(c.get("sector_code", ""))
            ticker = str(c.get("ticker", ""))
            t_evs  = ticker_events.get(ticker, [])
            for ev in t_evs:
                if 0 <= ev.get("days_until", 9999) <= window_days:
                    sector_events.setdefault(sc, set()).add(ev.get("type", ""))

        triples = []
        for sc, types in sector_events.items():
            if len(types) >= _TRIPLE_THRESHOLD:
                triples.append({
                    "sector_code":    sc,
                    "types":          list(types),
                    "catalyst_count": len(types),
                })

        return sorted(triples, key=lambda x: -x["catalyst_count"])

    except Exception:
        return []


# ==============================================================
# BLOCK 6: メインエントリポイント
# ==============================================================

def run_catalyst_calendar(
    cfg=None,
    results: Optional[dict] = None,
) -> dict:
    """
    Catalyst Calendar のメインエントリポイント。
    stock_analyzer_main.py の WEEKLY_MODULES から呼ばれる。

    処理フロー:
      1. グローバルカレンダー（中央銀行・経済指標）を構築
      2. screener 通過銘柄の決算日を取得（yfinance）
      3. 各銘柄の ticker_events に proximity_score を付与
      4. Triple Catalyst 検出
      5. catalyst_calendar.json / catalyst_history.json を保存
      6. 結果を返す

    Args:
        cfg:     config モジュール（None 可）
        results: stock_analyzer_main.results 辞書（None 可）

    Returns:
        {
          "global_events":     list,   # 中央銀行・経済指標予定
          "ticker_events":     dict,   # {ticker: [catalyst_event, ...]}
          "triple_tickers":    list,   # Triple Catalyst 銘柄
          "triple_sectors":    list,   # Triple Catalyst セクター
          "upcoming_14d":      list,   # 14日以内のイベント
          "summary":           str,
          "generated":         str,
        }
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    print("  📅 [catalyst_calendar] カタリストカレンダー更新中...")

    if results is None:
        results = {}

    # ── ① キャッシュ確認 ─────────────────────────────────────
    cached = _load_calendar_cache(cfg)
    if cached:
        print("    キャッシュ使用（本日分）")
        # Triple Catalyst は毎回再計算
        try:
            triples_t = detect_triple_catalyst_tickers(cached)
            cached["triple_tickers"] = triples_t
        except Exception:
            pass
        return cached

    # ── ② グローバルカレンダー構築 ───────────────────────────
    try:
        global_events = build_global_calendar(cfg)
        upcoming_14d  = [e for e in global_events if 0 <= e.get("days_until", 9999) <= 14]
        print(f"    グローバルイベント: {len(global_events)}件"
              f"（14日以内: {len(upcoming_14d)}件）")
    except Exception as e:
        print(f"    ⚠️  カレンダー構築エラー: {str(e)[:60]}")
        global_events = []
        upcoming_14d  = []

    # ── ③ screener 通過銘柄の決算日取得 ─────────────────────
    ticker_events: dict[str, list[dict]] = {}
    try:
        screener  = results.get("screener", {})
        passed    = (screener.get("passed")
                     or screener.get("candidates", []))
        tickers   = [str(c.get("ticker", "")) for c in passed if c.get("ticker")]

        if tickers:
            print(f"    決算日取得中: {len(tickers)}銘柄...")
            earnings_map = fetch_earnings_dates(tickers[:50], cfg)   # 上位50件

            for ticker, earnings_date in earnings_map.items():
                t_events: list[dict] = []

                if earnings_date:
                    du   = _days_until(earnings_date)
                    prox = calc_proximity_score(du, cfg)
                    if prox > 0:
                        t_events.append({
                            "date":            earnings_date,
                            "type":            "earnings",
                            "subtype":         "quarterly_earnings",
                            "description":     f"決算発表予定日",
                            "days_until":      du,
                            "proximity_score": prox,
                        })

                # セクター由来グローバルイベントを追加
                c_data = next(
                    (c for c in passed if str(c.get("ticker","")) == ticker), {}
                )
                sc = str(c_data.get("sector_code", ""))
                affinity = _SECTOR_ECONOMIC_AFFINITY.get(sc, [])
                for gev in global_events:
                    if gev.get("type") in affinity:
                        du   = gev.get("days_until", 9999)
                        prox = calc_proximity_score(du, cfg)
                        if prox > 0:
                            t_events.append({
                                **gev,
                                "proximity_score": prox,
                            })

                if t_events:
                    ticker_events[ticker] = sorted(
                        t_events, key=lambda x: x.get("days_until", 9999)
                    )

            print(f"    ticker_events 構築完了: {len(ticker_events)}銘柄")
    except Exception as e:
        print(f"    ⚠️  決算日取得エラー: {str(e)[:60]}")

    # ── ④ Triple Catalyst 検出 ───────────────────────────────
    result_data = {
        "global_events": global_events,
        "ticker_events": ticker_events,
        "upcoming_14d":  upcoming_14d,
    }
    try:
        triples_t = detect_triple_catalyst_tickers(result_data)
        triples_s = detect_triple_catalyst_sectors(
            result_data, passed if "passed" in dir() else [], cfg=cfg
        )
        if triples_t:
            print(f"    ⭐ Triple Catalyst銘柄: {len(triples_t)}件")
            for t in triples_t[:3]:
                print(f"      [{t['ticker']}] "
                      f"{'+'.join(t['types'])} "
                      f"最短{t['min_days']}日")
        if triples_s:
            print(f"    ⭐ Triple Catalyst セクター: {len(triples_s)}件")
    except Exception as e:
        triples_t, triples_s = [], []
        print(f"    ⚠️  Triple Catalyst検出エラー: {str(e)[:60]}")

    # ── ⑤ catalyst_history 更新 ──────────────────────────────
    try:
        history = _load_catalyst_history(cfg)
        today   = datetime.now().strftime("%Y-%m-%d")
        for t in triples_t:
            history.append({
                "date":      today,
                "ticker":    t["ticker"],
                "type":      "triple_catalyst",
                "triggered": True,
                "count":     t["catalyst_count"],
            })
        _save_catalyst_history(history, cfg)
    except Exception:
        pass

    # ── ⑥ 結果を構築・保存 ───────────────────────────────────
    summary = (
        f"グローバル{len(global_events)}件"
        f"（14日以内{len(upcoming_14d)}件） "
        f"銘柄イベント{len(ticker_events)}件 "
        f"Triple:{len(triples_t)}銘柄/{len(triples_s)}セクター"
    )
    output = {
        "global_events":  global_events,
        "ticker_events":  ticker_events,
        "triple_tickers": triples_t,
        "triple_sectors": triples_s,
        "upcoming_14d":   upcoming_14d,
        "summary":        summary,
        "generated":      datetime.now().strftime("%Y-%m-%d"),
    }

    _save_calendar_cache(output, cfg)
    print(f"  ✅ カタリストカレンダー完了: {summary}")
    return output


def load_catalyst_calendar(cfg=None) -> dict:
    """
    最新の catalyst_calendar.json を返す簡易ラッパー。
    screener / alert_notifier から参照する。
    """
    try:
        cached = _load_calendar_cache(cfg)
        return cached if cached else {}
    except Exception:
        return {}


# ==============================================================
# 単体テスト
# ==============================================================

if __name__ == "__main__":
    import tempfile

    print("=" * 60)
    print("  catalyst_calendar.py v1.0 単体テスト")
    print("=" * 60)

    class _TmpCfg:
        CACHE_DIR                 = Path(tempfile.mkdtemp())
        CATALYST_BONUS_MAX        = 5.0
        CATALYST_PROXIMITY_DAYS   = [14, 30, 60]
        CATALYST_PROXIMITY_SCORES = [1.0, 0.6, 0.3]
        CATALYST_CALENDAR_TTL_DAYS = 30
        CATALYST_HISTORY_TTL_DAYS  = 365

    _cfg = _TmpCfg()

    # ── proximity_score テスト ────────────────────────────────
    print("\n── proximity_score テスト ──")
    for days in [-1, 0, 7, 14, 20, 30, 45, 60, 90]:
        score = calc_proximity_score(days, _cfg)
        bar   = "▓" * int(score * 10)
        print(f"  day={days:3d}: score={score:.1f} {bar}")

    # ── グローバルカレンダー構築テスト ───────────────────────
    print("\n── グローバルカレンダー（今後60日以内）──")
    global_evs = build_global_calendar(_cfg)
    near_evs   = [e for e in global_evs if 0 <= e["days_until"] <= 60]
    for e in near_evs[:8]:
        print(f"  [{e['date']}] ({e['days_until']:2d}日後)"
              f" {e['type']:14s} {e['description'][:30]}")

    # ── catalyst_bonus テスト ────────────────────────────────
    print("\n── catalyst_bonus テスト ──")
    mock_data = {
        "global_events": global_evs,
        "ticker_events": {
            "6758.T": [
                {"date": (datetime.now() + timedelta(days=8)).strftime("%Y-%m-%d"),
                 "type": "earnings", "days_until": 8, "proximity_score": 1.0},
                {"date": (datetime.now() + timedelta(days=12)).strftime("%Y-%m-%d"),
                 "type": "central_bank", "days_until": 12, "proximity_score": 1.0},
                {"date": (datetime.now() + timedelta(days=10)).strftime("%Y-%m-%d"),
                 "type": "economic_data", "days_until": 10, "proximity_score": 1.0},
            ],
            "9432.T": [
                {"date": (datetime.now() + timedelta(days=25)).strftime("%Y-%m-%d"),
                 "type": "earnings", "days_until": 25, "proximity_score": 0.6},
            ],
        }
    }
    for ticker, sc in [("6758.T", "1016"), ("9432.T", "1025"), ("7203.T", "1017")]:
        bonus = calc_catalyst_bonus(ticker, sc, mock_data, _cfg)
        print(f"  [{ticker}] bonus={bonus:.2f}pt")

    # ── Triple Catalyst 検出テスト ────────────────────────────
    print("\n── Triple Catalyst 検出テスト ──")
    triples = detect_triple_catalyst_tickers(mock_data)
    print(f"  Triple Catalyst 銘柄数: {len(triples)}")
    for t in triples:
        print(f"  [{t['ticker']}] types={t['types']} "
              f"count={t['catalyst_count']} min_days={t['min_days']}")

    # ── キャッシュ保存・読み込みテスト ───────────────────────
    print("\n── キャッシュ I/O テスト ──")
    _save_calendar_cache(mock_data, _cfg)
    loaded = _load_calendar_cache(_cfg)
    print(f"  保存後ロード: global_events={len(loaded.get('global_events',[]))}件")
    print(f"  ticker_events={len(loaded.get('ticker_events',{}))}銘柄")

    print("\n✅ 単体テスト完了")
