# ==============================================================
# fed_calendar_fetcher.py  v1.0  (2-5 / F-3)
# ==============================================================
# 【概要】
#   CME FedWatch から次回FOMC利下げ確率を取得し、
#   policy_tracker.py に統合する。
#   日銀金融政策決定会合スケジュールも管理する。
#
# 【データソース優先順】
#   1. CME FedWatch 非公式 JSON（requests scrape）
#   2. FRED API: Fed Funds Futures 先物価格から逆算
#   3. Fallback: 取得不可の場合は確率=None で返す
#
# 【出力例】
#   {
#     "fed": {
#       "next_meeting_date": "2026-03-19",
#       "cut_prob_25bp":     32.5,    # 25bp利下げ確率(%)
#       "hold_prob":         61.8,    # 据え置き確率(%)
#       "hike_prob_25bp":     5.7,    # 25bp利上げ確率(%)
#       "current_rate":       4.375,  # 現在のFFレート中央値
#       "rate_bias":          "hold", # "cut"/"hold"/"hike"
#       "source":             "cme_fedwatch",
#     },
#     "boj": {
#       "next_meeting_date": "2026-04-30",
#       "meetings_2026":     [...],   # 2026年全会合日程
#       "last_decision":     "据え置き（0.5%）",
#     },
#     "macro_adjustment": float,  # composite_scoreへの加算値（-10〜+5）
#     "generated_at":  str,
#   }
#
# 【composite_score への影響】
#   FED利下げ確率 >= 60%: 株式全般に+5点（緩和バイアス）
#   FED利下げ確率 >= 40%: +2点
#   FED利上げ確率 >= 40%: -8点（引き締めバイアス）
#   FED利上げ確率 >= 20%: -4点
#   日銀会合 7日以内: 不確実性 -2点（ボラティリティリスク）
#
# 【依存】requests / config.py（POLICY_FRED_API_KEY）
# 【呼出】from fed_calendar_fetcher import run_fed_calendar
# ==============================================================

from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional


# ==============================================================
# 定数
# ==============================================================

# CME FedWatch 非公式エンドポイント
# FedWatchツール: https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html
CME_FEDWATCH_URL = (
    "https://www.cmegroup.com/CmeWS/mvc/SeasonalProducts/seasonalChart.html"
)
# 実際のFedWatch JSON API（30日先物データ）
CME_FF_CONTRACT_URL = (
    "https://www.cmegroup.com/CmeWS/mvc/Quotes/ContractsByNumber"
    "?productId=305&contractsNumber=6&isProtected"
)

# FRED: Fed Funds Futures (30日物先物)
FRED_FEDFUNDS_URL = "https://api.stlouisfed.org/fred/series/observations"

# 日銀会合スケジュール（2026年・公開情報）
BOJ_MEETINGS_2026 = [
    "2026-01-23",
    "2026-01-24",   # 2日間会合: 24日決定
    "2026-03-18",
    "2026-03-19",   # 決定: 19日
    "2026-04-30",
    "2026-05-01",   # 決定: 5/1
    "2026-06-16",
    "2026-06-17",   # 決定: 17日
    "2026-07-29",
    "2026-07-30",   # 決定: 30日
    "2026-09-18",
    "2026-09-19",   # 決定: 19日
    "2026-10-29",
    "2026-10-30",   # 決定: 30日
    "2026-12-17",
    "2026-12-18",   # 決定: 18日
]
# 各会合の「決定日」（2日目）
BOJ_DECISION_DATES_2026 = [
    "2026-01-24",
    "2026-03-19",
    "2026-05-01",
    "2026-06-17",
    "2026-07-30",
    "2026-09-19",
    "2026-10-30",
    "2026-12-18",
]

# FOMC 予定（2026年・公開情報）
# 2026-09-14: FRB公式（federalreserve.gov/monetarypolicy/fomccalendars.htm）と
# 照合して是正。旧リストは 8回中6回（1・3・4/5・6・10/11・12月）が
# 1日〜2週間ずれていた。catalyst_calendar もこのリストを正本として
# 議決日（初日＋1日）を組み立てるので、変更時はここだけ直せばよい。
FOMC_MEETINGS_2026 = [
    "2026-01-27",   # 27-28日, 決定: 28日
    "2026-03-17",   # 17-18日, 決定: 18日（SEP）
    "2026-04-28",   # 28-29日, 決定: 29日
    "2026-06-16",   # 16-17日, 決定: 17日（SEP）
    "2026-07-28",   # 28-29日, 決定: 29日
    "2026-09-15",   # 15-16日, 決定: 16日（SEP）
    "2026-10-27",   # 27-28日, 決定: 28日
    "2026-12-08",   # 8-9日,   決定: 9日（SEP）
]

# composite_score調整パラメータ
FED_ADJ_TABLE = [
    # (cut_prob_min, hike_prob_min, adjustment, label)
    (60, 0,   5.0,  "利下げ確率高（緩和バイアス）"),
    (40, 0,   2.0,  "利下げ確率中（やや緩和）"),
    (0,  40, -8.0,  "利上げ確率高（引き締めバイアス）"),
    (0,  20, -4.0,  "利上げ確率中（やや引き締め）"),
]
from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'B4_FED_CALENDAR_FETCHER_BOJ_PROXIMITY_DAYS': 'BOJ_PROXIMITY_DAYS',
}

BOJ_PROXIMITY_DAYS = 7   # 会合7日以内
BOJ_PROXIMITY_ADJ  = -2.0  # 会合接近による不確実性ペナルティ


# ==============================================================
# BLOCK 1: CME FedWatch データ取得
# ==============================================================

def _fetch_cme_fedwatch(timeout: int = 10) -> Optional[dict]:
    """
    CME FedWatch の先物価格から利下げ/据え置き/利上げ確率を計算する。

    CME FedWatch の仕組み:
      30日物FFレート先物価格 = 100 - 期待FFレート
      複数の満期契約の価格差から次回FOMC会合のレート変化確率を逆算する。

    Returns:
        {
          "cut_prob_25bp":  float,   # 25bp利下げ確率(%)
          "hold_prob":      float,   # 据え置き確率(%)
          "hike_prob_25bp": float,   # 25bp利上げ確率(%)
          "current_rate":   float,   # 現在のFFレート(%)
          "source":         str,
        }
        取得失敗時は None
    """
    try:
        import requests
    except ImportError:
        return None

    # --- 方法1: CME FF Futures JSON API ---
    try:
        headers = {
            "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36"),
            "Accept": "application/json",
            "Referer": "https://www.cmegroup.com/",
        }
        r = requests.get(CME_FF_CONTRACT_URL, headers=headers, timeout=timeout)
        if r.status_code == 200:
            data = r.json()
            quotes = data.get("quotes", [])
            if quotes:
                # 直近2契約の価格から確率を逆算
                prices = []
                for q in quotes[:2]:
                    last = q.get("last") or q.get("settle")
                    if last:
                        try:
                            prices.append(float(last))
                        except (TypeError, ValueError):
                            pass
                if len(prices) >= 2:
                    # 先物価格 = 100 - 期待FFレート中央値
                    implied_next = 100 - prices[1]
                    implied_curr = 100 - prices[0]
                    rate_diff    = round(implied_next - implied_curr, 4)

                    # 25bp単位で確率を逆算
                    if abs(rate_diff) < 0.05:
                        hold_prob = 100.0
                        cut_prob  = 0.0
                        hike_prob = 0.0
                    elif rate_diff < 0:  # 利下げ方向
                        cut_prob  = min(100.0, abs(rate_diff) / 0.25 * 100)
                        hold_prob = max(0.0, 100 - cut_prob)
                        hike_prob = 0.0
                    else:  # 利上げ方向
                        hike_prob = min(100.0, rate_diff / 0.25 * 100)
                        hold_prob = max(0.0, 100 - hike_prob)
                        cut_prob  = 0.0

                    return {
                        "cut_prob_25bp":  round(cut_prob, 1),
                        "hold_prob":      round(hold_prob, 1),
                        "hike_prob_25bp": round(hike_prob, 1),
                        "current_rate":   round(implied_curr, 3),
                        "source":         "cme_futures",
                    }
    except Exception as e:
        print(f"    ⚠️  CME Futures API失敗: {str(e)[:60]}")

    return None


def _fetch_via_yfinance() -> Optional[dict]:
    """
    yfinance の FF先物（ZQ）価格から利下げ確率を計算する。
    CME API が失敗した場合のフォールバック。

    ZQ = 30日物 FF 先物。ティッカー例: ZQH26 (2026年3月), ZQJ26 (4月)
    """
    try:
        import yf_cache as yf
        from datetime import date

        today = date.today()
        # 直近2〜3契約を試行
        month_codes = "FGHJKMNQUVXZ"  # 1〜12月のCMEコード
        year_short  = str(today.year)[-2:]

        tickers_to_try = []
        for i in range(3):
            m = today.month + i
            if m > 12:
                m -= 12
                yr = str(today.year + 1)[-2:]
            else:
                yr = year_short
            tickers_to_try.append(f"ZQ{month_codes[m-1]}{yr}.CBT")

        prices = []
        for t in tickers_to_try:
            try:
                info = yf.Ticker(t).info
                last = info.get("regularMarketPrice") or info.get("previousClose")
                if last:
                    prices.append(float(last))
                    if len(prices) >= 2:
                        break
                time.sleep(0.3)
            except Exception:
                continue

        if len(prices) >= 2:
            implied_curr = 100 - prices[0]
            implied_next = 100 - prices[1]
            rate_diff    = round(implied_next - implied_curr, 4)

            if abs(rate_diff) < 0.05:
                hold_prob = 100.0; cut_prob = 0.0; hike_prob = 0.0
            elif rate_diff < 0:
                cut_prob  = min(100.0, abs(rate_diff) / 0.25 * 100)
                hold_prob = max(0.0, 100 - cut_prob)
                hike_prob = 0.0
            else:
                hike_prob = min(100.0, rate_diff / 0.25 * 100)
                hold_prob = max(0.0, 100 - hike_prob)
                cut_prob  = 0.0

            return {
                "cut_prob_25bp":  round(cut_prob, 1),
                "hold_prob":      round(hold_prob, 1),
                "hike_prob_25bp": round(hike_prob, 1),
                "current_rate":   round(implied_curr, 3),
                "source":         "yfinance_zq",
            }
    except Exception as e:
        print(f"    ⚠️  yfinance ZQ取得失敗: {str(e)[:60]}")

    return None


def _fetch_via_fred(api_key: str) -> Optional[dict]:
    """
    FRED API から FEDFUNDS（実績値）とFFレート目標帯を取得する。
    確率は算出できないため、バイアス判定のみ返す。

    Returns:
        {"current_rate": float, "source": "fred", 確率キーはNone}
    """
    if not api_key:
        return None
    try:
        import requests
        params = {
            "series_id":        "FEDFUNDS",
            "api_key":           api_key,
            "file_type":        "json",
            "sort_order":       "desc",
            "limit":            "1",
            "observation_start": (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d"),
        }
        r = requests.get(FRED_FEDFUNDS_URL, params=params, timeout=8)
        if r.status_code == 200:
            obs = r.json().get("observations", [])
            if obs:
                try:
                    rate = float(obs[0]["value"])
                    return {
                        "cut_prob_25bp":  None,
                        "hold_prob":      None,
                        "hike_prob_25bp": None,
                        "current_rate":   round(rate, 3),
                        "source":         "fred_fedfunds",
                    }
                except (ValueError, TypeError):
                    pass
    except Exception as e:
        print(f"    ⚠️  FRED FEDFUNDS失敗: {str(e)[:60]}")
    return None


# ==============================================================
# BLOCK 2: 日銀会合スケジュール管理
# ==============================================================

def get_boj_schedule(reference_date: Optional[date] = None) -> dict:
    """
    日銀金融政策決定会合のスケジュール情報を返す。

    Returns:
        {
          "next_meeting_date":  str,   # 次回決定会合日 (YYYY-MM-DD)
          "days_until_meeting": int,   # 今日から何日後か
          "meetings_remaining": int,   # 今年残り会合数
          "is_proximity":       bool,  # 7日以内に会合があるか
          "decision_dates_2026": [str],
        }
    """
    today = reference_date or date.today()
    today_str = today.strftime("%Y-%m-%d")

    future_dates = [d for d in BOJ_DECISION_DATES_2026 if d >= today_str]
    next_meeting  = future_dates[0] if future_dates else None
    days_until    = None

    if next_meeting:
        nm = datetime.strptime(next_meeting, "%Y-%m-%d").date()
        days_until = (nm - today).days

    return {
        "next_meeting_date":   next_meeting,
        "days_until_meeting":  days_until,
        "meetings_remaining":  len(future_dates),
        "is_proximity":        (days_until is not None and days_until <= BOJ_PROXIMITY_DAYS),
        "decision_dates_2026": BOJ_DECISION_DATES_2026,
    }


def get_fomc_schedule(reference_date: Optional[date] = None) -> dict:
    """
    FOMC スケジュール情報を返す。

    Returns:
        {
          "next_meeting_date":  str,
          "days_until_meeting": int,
          "meetings_remaining": int,
          "is_proximity":       bool,   # 7日以内
        }
    """
    today = reference_date or date.today()
    today_str = today.strftime("%Y-%m-%d")

    future_dates = [d for d in FOMC_MEETINGS_2026 if d >= today_str]
    next_meeting  = future_dates[0] if future_dates else None
    days_until    = None

    if next_meeting:
        nm = datetime.strptime(next_meeting, "%Y-%m-%d").date()
        days_until = (nm - today).days

    return {
        "next_meeting_date":   next_meeting,
        "days_until_meeting":  days_until,
        "meetings_remaining":  len(future_dates),
        "is_proximity":        (days_until is not None and days_until <= BOJ_PROXIMITY_DAYS),
    }


# ==============================================================
# BLOCK 3: composite_score 調整値計算
# ==============================================================

def calc_fed_macro_adjustment(fed_data: dict, boj_data: dict) -> tuple[float, str]:
    """
    FED利下げ確率と日銀会合接近度から composite_score 調整値を計算する。

    Args:
        fed_data: _fetch_cme_fedwatch() 等の戻り値
        boj_data: get_boj_schedule() の戻り値

    Returns:
        (adjustment: float, rationale: str)
    """
    adjustment = 0.0
    rationale_parts = []

    cut_prob  = fed_data.get("cut_prob_25bp")
    hike_prob = fed_data.get("hike_prob_25bp")

    if cut_prob is not None and hike_prob is not None:
        for cut_min, hike_min, adj, label in FED_ADJ_TABLE:
            if cut_min > 0 and cut_prob >= cut_min:
                adjustment = adj
                rationale_parts.append(f"FED利下げ確率{cut_prob:.0f}%→{adj:+.0f}pt")
                break
            elif hike_min > 0 and hike_prob >= hike_min:
                adjustment = adj
                rationale_parts.append(f"FED利上げ確率{hike_prob:.0f}%→{adj:+.0f}pt")
                break

    # 日銀会合接近ペナルティ
    if boj_data.get("is_proximity"):
        days = boj_data.get("days_until_meeting", 0)
        adjustment += BOJ_PROXIMITY_ADJ
        rationale_parts.append(f"日銀会合{days}日前→不確実性{BOJ_PROXIMITY_ADJ:+.0f}pt")

    rationale = " / ".join(rationale_parts) if rationale_parts else "中立（調整なし）"
    return round(adjustment, 1), rationale


# ==============================================================
# BLOCK 4: キャッシュ管理
# ==============================================================

def _load_cache(cache_path: Path) -> Optional[dict]:
    """1日有効なキャッシュを読み込む。"""
    try:
        if not cache_path.exists():
            return None
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        cached_date = data.get("cache_date", "")
        if cached_date == datetime.now().strftime("%Y-%m-%d"):
            return data
    except Exception:
        pass
    return None


def _save_cache(cache_path: Path, data: dict) -> None:
    """結果をキャッシュに保存する。"""
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        data["cache_date"] = datetime.now().strftime("%Y-%m-%d")
        cache_path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    except Exception:
        pass


# ==============================================================
# BLOCK 5: メインエントリーポイント
# ==============================================================

def run_fed_calendar(cfg) -> dict:
    """
    FED/BOJカレンダーを取得してcomposite_score調整値を返す。

    stock_analyzer_main.py から以下のように呼び出す:
        from fed_calendar_fetcher import run_fed_calendar
        fed_result = run_fed_calendar(cfg)
        results["fed_calendar"] = fed_result

    Args:
        cfg: config モジュール（POLICY_FRED_API_KEY, DATA_DIR を参照）

    Returns:
        {
          "fed":              dict,   # FED利下げ確率等
          "boj":              dict,   # 日銀会合スケジュール
          "fomc":             dict,   # FOMC スケジュール
          "macro_adjustment": float,  # composite_scoreへの加算値
          "adjustment_rationale": str,
          "generated_at":     str,
        }
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    print("\n  📅 FEDカレンダー取得中...")

    cache_path = Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / "fed_calendar_cache.json"
    cached = _load_cache(cache_path)
    if cached:
        print(f"    ✅ キャッシュ使用（{cached.get('cache_date')}）")
        return cached

    fred_api_key = getattr(cfg, "POLICY_FRED_API_KEY", "")

    # FED利下げ確率取得（優先順: CME → yfinance → FRED → None）
    fed_probs = None
    for fetcher, name in [
        (_fetch_cme_fedwatch,      "CME FedWatch"),
        (_fetch_via_yfinance,      "yfinance ZQ"),
        (lambda: _fetch_via_fred(fred_api_key), "FRED FEDFUNDS"),
    ]:
        try:
            fed_probs = fetcher()
            if fed_probs:
                print(f"    ✅ FED確率取得: {name} "
                      f"(利下げ={fed_probs.get('cut_prob_25bp','N/A')}%"
                      f" 据え置き={fed_probs.get('hold_prob','N/A')}%"
                      f" 利上げ={fed_probs.get('hike_prob_25bp','N/A')}%)")
                break
        except Exception as e:
            print(f"    ⚠️  {name} 失敗: {str(e)[:60]}")

    if not fed_probs:
        fed_probs = {
            "cut_prob_25bp":  None,
            "hold_prob":      None,
            "hike_prob_25bp": None,
            "current_rate":   None,
            "source":         "unavailable",
        }
        print("    ⚠️  FED確率: 全ソース取得失敗（Noneで返す）")

    # 会合スケジュール
    boj_schedule  = get_boj_schedule()
    fomc_schedule = get_fomc_schedule()

    if boj_schedule["next_meeting_date"]:
        print(f"    📆 次回日銀会合: {boj_schedule['next_meeting_date']} "
              f"（{boj_schedule['days_until_meeting']}日後）")
    if fomc_schedule["next_meeting_date"]:
        print(f"    📆 次回FOMC:    {fomc_schedule['next_meeting_date']} "
              f"（{fomc_schedule['days_until_meeting']}日後）")

    # composite_score 調整値
    adj, adj_reason = calc_fed_macro_adjustment(fed_probs, boj_schedule)

    result = {
        "fed":                    fed_probs,
        "boj":                    boj_schedule,
        "fomc":                   fomc_schedule,
        "macro_adjustment":       adj,
        "adjustment_rationale":   adj_reason,
        "generated_at":           datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }

    _save_cache(cache_path, result)
    print(f"    ✅ FEDカレンダー完了: 調整値={adj:+.1f}pt（{adj_reason}）")
    return result


# ==============================================================
# BLOCK 6: テキスト整形（アラート・週次レポート用）
# ==============================================================

def format_fed_calendar_text(result: dict) -> str:
    """
    run_fed_calendar() の結果をテキスト形式に整形する。

    出力例:
      🏛️ FED / BOJ カレンダー
      FED: 利下げ32.5% | 据え置き61.8% | 利上げ5.7%（CME FedWatch）
      次回FOMC: 2026-03-19（5日後）
      次回日銀: 2026-05-01（47日後）
      調整値: +2pt（利下げ確率40%以上）
    """
    fed   = result.get("fed", {})
    boj   = result.get("boj", {})
    fomc  = result.get("fomc", {})
    adj   = result.get("macro_adjustment", 0)
    reason = result.get("adjustment_rationale", "")

    lines = ["🏛️ FED / BOJ カレンダー"]

    cut  = fed.get("cut_prob_25bp")
    hold = fed.get("hold_prob")
    hike = fed.get("hike_prob_25bp")
    src  = fed.get("source", "N/A")
    if cut is not None:
        lines.append(f"  FED: 利下げ{cut:.1f}% | 据え置き{hold:.1f}% | 利上げ{hike:.1f}%（{src}）")
    else:
        lines.append(f"  FED確率: 取得不可（{src}）")

    if fomc.get("next_meeting_date"):
        lines.append(f"  次回FOMC: {fomc['next_meeting_date']}（{fomc['days_until_meeting']}日後）")
    if boj.get("next_meeting_date"):
        proximity_flag = "⚠️ 接近中" if boj["is_proximity"] else ""
        lines.append(f"  次回日銀: {boj['next_meeting_date']}（{boj['days_until_meeting']}日後）{proximity_flag}")

    sign = "+" if adj >= 0 else ""
    lines.append(f"  調整値: {sign}{adj:.1f}pt（{reason}）")

    return "\n".join(lines)
