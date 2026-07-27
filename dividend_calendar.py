# ==============================================================
# dividend_calendar.py  v1.0  (4-3 / A-4)
# 権利確定日カレンダー + 30日以内権利日の Gmail 通知
# ==============================================================
# 【役割】
#   yfinance から配当・株式分割の権利確定日を取得し、
#   「30日以内に権利確定が来る保有銘柄」を Gmail で通知する。
#
# 【検出内容】
#   A. 権利確定日（Ex-Dividend Date）
#      この日までに保有していれば配当が受け取れる
#
#   B. 配当落ち予定日（Dividend Date / 実際の支払日）
#
#   C. 株式分割（Stock Split）予定日（あれば）
#
#   D. 次回決算発表予定日（Earnings Date）
#      決算跨ぎリスクの確認用
#
# 【通知条件】
#   30日以内に権利確定日がある銘柄 → Gmail 通知
#   7日以内の場合 → 緊急フラグ付き
#
# 【配当スコア（screener 統合）】
#   権利確定まで 1〜30日: quality_score +3pt（配当取り機会）
#   権利落ち直後（-5〜0日）: -2pt（株価下落リスク）
#
# 【出力スキーマ】
#   {
#     "dividends":        [DividendEvent, ...],   # 全配当イベント
#     "upcoming_30d":     [DividendEvent, ...],   # 30日以内
#     "urgent_7d":        [DividendEvent, ...],   # 7日以内（緊急）
#     "bonuses":          {ticker: float},        # screener ボーナス
#     "total":            int,
#     "alert_text":       str,
#     "timestamp":        str,
#   }
#   DividendEvent = {
#     "ticker":           str,
#     "name":             str,
#     "ex_date":          str,    # 権利確定日 YYYY-MM-DD
#     "dividend_amount":  float,  # 1株あたり配当額（JPY or USD）
#     "yield_pct":        float,  # 配当利回り%
#     "days_until":       int,    # 今日から何日後か（負=過去）
#     "earnings_date":    str,    # 次回決算日（あれば）
#     "market":           str,    # "JP" / "US"
#   }
#
# 【依存】yfinance / config.py / portfolio_holdings.csv
# 【呼出】from dividend_calendar import run_dividend_calendar, get_dividend_bonus
# ==============================================================

from __future__ import annotations

import csv
import json
import time
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Optional

# ── 定数 ──────────────────────────────────────────────────────
ALERT_WINDOW_DAYS  = 30   # 通知対象: N日以内
URGENT_WINDOW_DAYS = 7    # 緊急通知: N日以内
FETCH_DELAY        = 0.3  # yfinance レート制限
CACHE_HOURS        = 24   # 日次キャッシュ（権利日は毎日変わらないが配当額は変わりうる）
CACHE_FILENAME     = "dividend_calendar_cache.json"

# screener ボーナス

from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'DC_BONUS_UPCOMING_30D': 'BONUS_UPCOMING_30D',
    'DC_PENALTY_POST_EX': 'PENALTY_POST_EX',
    'B4_DIVIDEND_CALENDAR_ALERT_WINDOW_DAYS': 'ALERT_WINDOW_DAYS',
    'B4_DIVIDEND_CALENDAR_URGENT_WINDOW_DAYS': 'URGENT_WINDOW_DAYS',
    'B4_DIVIDEND_CALENDAR_CACHE_HOURS': 'CACHE_HOURS',
    'B5_DIVIDEND_CALENDAR_FETCH_DELAY': 'FETCH_DELAY',
}

BONUS_UPCOMING_30D   =  3.0   # 30日以内権利確定
PENALTY_POST_EX      = -2.0   # 権利落ち直後（-5〜0日）


# ==============================================================
# BLOCK 1: ポートフォリオ読み込み
# ==============================================================

def _load_portfolio_tickers(cfg) -> list:
    """portfolio_holdings.csv から保有銘柄リストを取得する。"""
    candidates = [
        Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / "portfolio_holdings.csv",
        Path(__file__).parent / "portfolio_holdings.csv",
    ]
    for p in candidates:
        if not p.exists():
            continue
        try:
            result = []
            with open(p, encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    t = row.get("ticker", "").strip()
                    if t:
                        result.append({
                            "ticker": t,
                            "name":   row.get("name", t),
                            "market": row.get("market", "JP"),
                        })
            return result
        except Exception:
            pass
    return []


# ==============================================================
# BLOCK 2: yfinance で配当情報取得
# ==============================================================

def _fetch_dividend_info(ticker: str, name: str, market: str) -> Optional[dict]:
    """
    1銘柄の配当カレンダー情報を取得する。

    yfinance のカレンダー情報:
      t.calendar: DataFrame（Ex-Dividend Date / Dividend Date 等）
      t.info:     forwardAnnualDividendYield / exDividendDate 等
    """
    try:
        import yfinance as yf

        t    = yf.Ticker(ticker)
        info = t.info or {}

        # Ex-Dividend Date (Epoch秒 → date)
        ex_date_epoch = info.get("exDividendDate")
        ex_date = None
        if ex_date_epoch:
            try:
                ex_date = datetime.fromtimestamp(int(ex_date_epoch)).date()
            except (TypeError, ValueError, OSError):
                pass

        # カレンダーから補完
        if ex_date is None:
            try:
                cal = t.calendar
                if cal is not None and not (hasattr(cal, 'empty') and cal.empty):
                    # DataFrame か dict かで処理を分ける
                    if hasattr(cal, 'loc'):
                        ex_val = cal.loc["Ex-Dividend Date"] if "Ex-Dividend Date" in cal.index else None
                        if ex_val is not None:
                            ex_date = ex_val.date() if hasattr(ex_val, 'date') else None
                    elif isinstance(cal, dict):
                        ex_val = cal.get("Ex-Dividend Date") or cal.get("exDividendDate")
                        if ex_val:
                            if hasattr(ex_val, 'date'):
                                ex_date = ex_val.date()
                            elif isinstance(ex_val, (int, float)):
                                ex_date = datetime.fromtimestamp(int(ex_val)).date()
            except Exception:
                pass

        if ex_date is None:
            return None

        today     = date.today()
        days_until = (ex_date - today).days

        # 配当額
        div_amount = info.get("lastDividendValue") or info.get("dividendRate") or 0
        div_yield  = info.get("dividendYield") or info.get("forwardDividendYield") or 0

        # 次回決算日
        earnings_date = None
        try:
            cal = t.calendar
            if cal is not None:
                if hasattr(cal, 'loc') and "Earnings Date" in cal.index:
                    ed = cal.loc["Earnings Date"]
                    earnings_date = str(ed[0].date()) if hasattr(ed, '__getitem__') else None
                elif isinstance(cal, dict):
                    ed_list = cal.get("Earnings Date") or []
                    if ed_list:
                        earnings_date = str(ed_list[0].date() if hasattr(ed_list[0], 'date') else ed_list[0])
        except Exception:
            pass

        return {
            "ticker":          ticker,
            "name":            name[:20],
            "market":          market,
            "ex_date":         str(ex_date),
            "dividend_amount": round(float(div_amount), 4) if div_amount else 0,
            "yield_pct":       round(float(div_yield) * 100, 2) if div_yield else 0,
            "days_until":      days_until,
            "earnings_date":   earnings_date,
        }

    except Exception:
        return None


# ==============================================================
# BLOCK 3: ボーナス算出
# ==============================================================

def _calc_dividend_bonus(days_until: int) -> float:
    """
    権利確定日までの日数からボーナスを算出する。

    days_until > 0: 権利確定前（買いチャンス）
    days_until <= 0 and >= -5: 権利落ち直後（株価下落リスク）
    """
    if 0 <= days_until <= ALERT_WINDOW_DAYS:
        return BONUS_UPCOMING_30D
    elif -5 <= days_until <= 0:
        return PENALTY_POST_EX
    return 0.0


def get_dividend_bonus(ticker: str, dividend_cache: dict) -> float:
    """
    stock_screener.py から呼び出すインターフェース。
    キャッシュからボーナスを返す。
    """
    bonuses = dividend_cache.get("bonuses", {})
    return float(bonuses.get(ticker, 0.0))


# ==============================================================
# BLOCK 4: テキスト整形 + Gmail HTML 生成
# ==============================================================

def _fmt_days(days: int) -> str:
    if days == 0:
        return "本日"
    elif days > 0:
        return f"{days}日後"
    else:
        return f"{abs(days)}日前（権利落ち済）"


def format_dividend_alert_text(result: dict) -> str:
    """週次レポート・LINE 向けテキストを生成する。"""
    upcoming = result.get("upcoming_30d", [])
    urgent   = result.get("urgent_7d", [])

    lines = [f"📅 配当権利確定カレンダー（30日以内: {len(upcoming)}銘柄）"]

    if urgent:
        lines.append(f"\n🚨 緊急（7日以内）:")
        for ev in urgent:
            lines.append(
                f"  {ev['ticker']} {ev['name'][:14]}: "
                f"{ev['ex_date']} ({_fmt_days(ev['days_until'])}) "
                f"配当{ev['dividend_amount']:.0f} 利回{ev['yield_pct']:.1f}%"
            )

    if upcoming:
        non_urgent = [e for e in upcoming if e not in urgent]
        if non_urgent:
            lines.append(f"\n📆 今後30日以内:")
            for ev in sorted(non_urgent, key=lambda x: x["days_until"])[:8]:
                lines.append(
                    f"  {ev['ticker']} {ev['name'][:14]}: "
                    f"{ev['ex_date']} ({_fmt_days(ev['days_until'])}) "
                    f"利回{ev['yield_pct']:.1f}%"
                )

    if not upcoming:
        lines.append("  今後30日以内に権利確定予定の銘柄なし")

    return "\n".join(lines)


def format_dividend_gmail_html(result: dict) -> str:
    """Gmail 通知用の HTML を生成する。"""
    upcoming = result.get("upcoming_30d", [])
    urgent   = result.get("urgent_7d", [])

    rows_urgent = ""
    for ev in urgent:
        rows_urgent += (
            f"<tr style='background:#fef3f2'>"
            f"<td style='padding:6px'><b>{ev['ticker']}</b></td>"
            f"<td>{ev['name'][:16]}</td>"
            f"<td style='color:#c0392b;font-weight:bold'>{ev['ex_date']}</td>"
            f"<td style='color:#c0392b;font-weight:bold'>{_fmt_days(ev['days_until'])}</td>"
            f"<td>{ev['dividend_amount']:.0f}</td>"
            f"<td>{ev['yield_pct']:.1f}%</td>"
            f"</tr>"
        )

    rows_normal = ""
    for ev in sorted(upcoming, key=lambda x: x["days_until"]):
        if ev in urgent:
            continue
        rows_normal += (
            f"<tr>"
            f"<td style='padding:6px'>{ev['ticker']}</td>"
            f"<td>{ev['name'][:16]}</td>"
            f"<td>{ev['ex_date']}</td>"
            f"<td>{_fmt_days(ev['days_until'])}</td>"
            f"<td>{ev['dividend_amount']:.0f}</td>"
            f"<td>{ev['yield_pct']:.1f}%</td>"
            f"</tr>"
        )

    all_rows = rows_urgent + rows_normal
    header_row = "<tr style='background:#2c3e50;color:white'><th>Ticker</th><th>銘柄名</th><th>権利確定日</th><th>残日数</th><th>配当額</th><th>利回り</th></tr>"

    html = f"""
<html><body style="font-family:sans-serif;max-width:600px;margin:auto;">
<h2 style="color:#2c3e50">📅 配当権利確定カレンダー</h2>
<p>30日以内に権利確定が予定されている保有銘柄: <b>{len(upcoming)}銘柄</b>
{'&emsp;🚨 うち <b style="color:#c0392b">'+str(len(urgent))+'銘柄</b>が7日以内' if urgent else ''}</p>

<table style="width:100%;border-collapse:collapse;border:1px solid #bdc3c7">
{header_row}
{all_rows if all_rows else '<tr><td colspan="6" style="padding:12px;text-align:center;color:#7f8c8d">今後30日以内に権利確定予定なし</td></tr>'}
</table>

<p style="color:#7f8c8d;font-size:12px;margin-top:16px">
⚠️ 権利確定日は yfinance から取得。実際の取引については証券会社にて確認してください。<br>
stock_analyzer — 自動生成 {datetime.now().strftime('%Y-%m-%d %H:%M')}
</p>
</body></html>
"""
    return html


# ==============================================================
# BLOCK 5: キャッシュ管理
# ==============================================================

def _cache_path(cfg) -> Path:
    return Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / CACHE_FILENAME

def _load_cache(cfg) -> Optional[dict]:
    try:
        p = _cache_path(cfg)
        if not p.exists():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        cached_at = datetime.fromisoformat(data.get("cached_at", "2000-01-01"))
        if datetime.now() - cached_at < timedelta(hours=CACHE_HOURS):
            return data
    except Exception:
        pass
    return None

def _save_cache(cfg, data: dict) -> None:
    try:
        p = _cache_path(cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        data["cached_at"] = datetime.now().isoformat()
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


# ==============================================================
# BLOCK 6: メインエントリポイント
# ==============================================================

def run_dividend_calendar(cfg, send_gmail_if_urgent: bool = True) -> dict:
    """
    保有銘柄の配当権利確定カレンダーを生成する。

    stock_analyzer_main.py から週次で呼び出す:
        _dc = _load_local_module("dividend_calendar", ...)
        dc_result = _dc.run_dividend_calendar(cfg)
        results["dividend_calendar"] = dc_result

    30日以内に権利確定がある銘柄を検出し、
    7日以内の場合は Gmail 緊急通知を送信する。

    Args:
        cfg:                  config モジュール
        send_gmail_if_urgent: True で 7日以内の場合 Gmail 送信

    Returns:
        dividend calendar result dict
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    print("\n  📅 配当カレンダー取得開始...")

    cached = _load_cache(cfg)
    if cached:
        upcoming = cached.get("upcoming_30d", [])
        print(f"    ✅ キャッシュ使用（{len(upcoming)}銘柄 / 30日以内）")
        return cached

    # ポートフォリオ読み込み
    portfolio = _load_portfolio_tickers(cfg)
    if not portfolio:
        # portfolio がなければ SECTOR_REPRESENTATIVE_TICKERS を使用
        rep = getattr(cfg, "SECTOR_REPRESENTATIVE_TICKERS", {})
        portfolio = [
            {"ticker": t, "name": t, "market": "JP"}
            for ts in rep.values() for t in ts[:1]
        ]

    print(f"    対象: {len(portfolio)}銘柄")

    dividends = []
    bonuses   = {}

    for item in portfolio:
        ticker = item["ticker"]
        info   = _fetch_dividend_info(ticker, item["name"], item["market"])
        if info:
            dividends.append(info)
            bonus = _calc_dividend_bonus(info["days_until"])
            if bonus != 0:
                bonuses[ticker] = bonus
        time.sleep(FETCH_DELAY)

    today  = date.today()
    upcoming_30d = sorted(
        [ev for ev in dividends if 0 <= ev["days_until"] <= ALERT_WINDOW_DAYS],
        key=lambda x: x["days_until"],
    )
    urgent_7d = [ev for ev in upcoming_30d if ev["days_until"] <= URGENT_WINDOW_DAYS]

    print(f"    ✅ 取得完了: 権利確定情報{len(dividends)}銘柄 "
          f"/ 30日以内{len(upcoming_30d)}銘柄 / 7日以内{len(urgent_7d)}銘柄")

    result = {
        "dividends":    dividends,
        "upcoming_30d": upcoming_30d,
        "urgent_7d":    urgent_7d,
        "bonuses":      bonuses,
        "total":        len(dividends),
        "timestamp":    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    result["alert_text"]   = format_dividend_alert_text(result)
    result["gmail_html"]   = format_dividend_gmail_html(result)

    # ── 緊急 Gmail 送信（7日以内） ────────────────────────────
    if send_gmail_if_urgent and urgent_7d:
        try:
            import smtplib
            from email.mime.multipart import MIMEMultipart
            from email.mime.text      import MIMEText

            frm = getattr(cfg, "ALERT_GMAIL_FROM",     "")
            pwd = getattr(cfg, "ALERT_GMAIL_PASSWORD",  "")
            to  = getattr(cfg, "ALERT_GMAIL_TO",        "")

            if frm and pwd and to:
                ticker_list = " / ".join(e["ticker"] for e in urgent_7d[:3])
                subject     = f"📅【権利確定7日以内】{ticker_list} 他{len(urgent_7d)}銘柄"

                msg = MIMEMultipart("alternative")
                msg["Subject"] = subject
                msg["From"]    = frm
                msg["To"]      = to
                msg.attach(MIMEText(result["gmail_html"], "html", "utf-8"))

                with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
                    smtp.login(frm, pwd)
                    smtp.sendmail(frm, to, msg.as_string())
                print(f"    ✅ 緊急 Gmail 送信: {subject[:60]}")
                result["mail_sent"] = True
            else:
                result["mail_sent"] = False
        except Exception as e:
            print(f"    ⚠️  Gmail 送信失敗: {str(e)[:60]}")
            result["mail_sent"] = False

    _save_cache(cfg, result)
    return result
