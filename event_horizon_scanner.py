# ==============================================================
# event_horizon_scanner.py  v1.0  (Wave EVT-1)
# イベントホライズン — 金融イベント・決算の事前アラート（日次）
# ==============================================================
# 【役割】
#   N日先（EVENT_HORIZON_ALERT_DAYS）までに迫った金融イベントを毎日
#   検出し、alert_notifier 経由で Gmail/LINE にカウントダウン通知する。
#
# 【イベント源（重複させない設計）】
#   ① マクロ日程  : config BLOCK 40 の公式日程表
#                   （FOMC/日銀/CPI/GDP/短観/PMI/雇用統計 — catalyst_calendar と共有）
#   ② SQ・OPEX    : アルゴリズム計算（API不要）
#                   日経SQ=毎月第2金曜（3/6/9/12月はメジャーSQ）、米OPEX=第3金曜
#                   → オプション取引の建玉整理・ロール判断に直結
#   ③ 企業決算    : catalyst_calendar.json キャッシュ（週次更新・screener銘柄）
#                   ＋ 保有銘柄（portfolio_holdings.csv）は yfinance で直接取得
#                   （TTLキャッシュで呼び出し抑制）
#   ※ 配当権利確定は dividend_calendar.py [4-3] が既に通知するため扱わない
#
# 【出力】
#   results["event_horizon"] = {"upcoming": [...], "alerts": [...], ...}
#   INSIGHT_DIR/events/event_horizon_latest.md（Obsidian同期対象）
#   alert_notifier が results["event_horizon"]["alerts"] を拾い配信
#
# 【依存】config.py (BLOCK 40) / catalyst_calendar.json / yfinance（任意）
# ==============================================================

from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

_EARNINGS_CACHE = "portfolio_earnings_cache.json"


def _today() -> datetime:
    return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)


def _days_until(date_str: str) -> Optional[int]:
    try:
        return (datetime.strptime(date_str, "%Y-%m-%d") - _today()).days
    except Exception:
        return None


# ==============================================================
# BLOCK 1: SQ / OPEX 日の算出（純粋ロジック・テスト対象）
# ==============================================================

def nth_friday(year: int, month: int, n: int) -> str:
    """指定月の第n金曜日を YYYY-MM-DD で返す。"""
    d = datetime(year, month, 1)
    fridays = 0
    while True:
        if d.weekday() == 4:  # 金曜
            fridays += 1
            if fridays == n:
                return d.strftime("%Y-%m-%d")
        d += timedelta(days=1)


def build_sq_events(months_ahead: int = 3) -> list[dict]:
    """日経SQ（第2金曜）と米OPEX（第3金曜）を今後 months_ahead ヶ月分生成。"""
    events = []
    base = _today()
    for i in range(months_ahead + 1):
        y = base.year + (base.month - 1 + i) // 12
        m = (base.month - 1 + i) % 12 + 1
        sq = nth_friday(y, m, 2)
        major = m in (3, 6, 9, 12)
        events.append({
            "date": sq,
            "type": "sq",
            "label": ("🇯🇵 メジャーSQ（先物・オプション同時satisfy）" if major
                      else "🇯🇵 オプションSQ"),
            "market": "JP",
            "importance": "high" if major else "medium",
        })
        events.append({
            "date": nth_friday(y, m, 3),
            "type": "opex",
            "label": "🇺🇸 米オプション満期（OPEX・第3金曜）",
            "market": "US",
            "importance": "medium",
        })
    return events


# ==============================================================
# BLOCK 2: イベント収集
# ==============================================================

_MACRO_SOURCES = [
    ("FED_FOMC_DATES",    "🏛 FOMC 金利決定",        "US", "high"),
    ("BOJ_MEETING_DATES", "🏛 日銀 政策決定会合",     "JP", "high"),
    ("US_CPI_DATES",      "📊 米CPI 発表",           "US", "high"),
    ("US_NFP_DATES",      "📊 米雇用統計",           "US", "high"),
    ("US_GDP_DATES",      "📊 米GDP 速報",           "US", "medium"),
    ("BOJ_TANKAN_DATES",  "📊 日銀短観",             "JP", "medium"),
    ("PMI_DATES",         "📊 PMI",                  "BOTH", "low"),
]


def collect_macro_events(cfg) -> list[dict]:
    events = []
    for attr, label, market, imp in _MACRO_SOURCES:
        for d in getattr(cfg, attr, []) or []:
            events.append({"date": d, "type": "macro", "label": label,
                           "market": market, "importance": imp})
    return events


def collect_earnings_events(cfg) -> list[dict]:
    """screener銘柄（catalystキャッシュ）＋保有銘柄（yfinance）の決算日。"""
    events = []
    # ① catalyst_calendar.json（週次更新済みキャッシュ・API呼び出しなし）
    try:
        p = Path(getattr(cfg, "CACHE_DIR", "stock_data/cache")) / "catalyst_calendar.json"
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            for tk, evs in (data.get("ticker_events", {}) or {}).items():
                for e in evs or []:
                    if "earnings" in str(e.get("subtype", e.get("type", ""))).lower():
                        d = str(e.get("date", ""))[:10]
                        if d:
                            events.append({
                                "date": d, "type": "earnings",
                                "label": f"📈 決算発表: {tk}",
                                "ticker": tk, "market": "",
                                "importance": "medium",
                            })
    except Exception:
        pass
    # ② 保有銘柄（最重要・yfinance直接 / TTLキャッシュ）
    events += _portfolio_earnings(cfg)
    return events


def _portfolio_earnings(cfg) -> list[dict]:
    cache_dir = Path(getattr(cfg, "CACHE_DIR", "stock_data/cache")) / "events"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_p = cache_dir / _EARNINGS_CACHE
    ttl = int(getattr(cfg, "EVENT_HORIZON_EARNINGS_TTL_DAYS", 3))

    # キャッシュ有効ならそれを使う
    try:
        cached = json.loads(cache_p.read_text(encoding="utf-8"))
        age = (_today() - datetime.strptime(cached["fetched"], "%Y-%m-%d")).days
        if age < ttl:
            return cached.get("events", [])
    except Exception:
        pass

    # portfolio_holdings.csv 読み込み
    tickers = []
    hp = Path(__file__).parent / "data" / "portfolio_holdings.csv"
    if hp.exists():
        for enc in ("utf-8", "cp932"):
            try:
                with open(hp, encoding=enc) as f:
                    tickers = [r.get("ticker", "") for r in csv.DictReader(f)
                               if r.get("ticker")]
                break
            except UnicodeDecodeError:
                continue
    events = []
    if tickers:
        try:
            import yf_cache as yf
            for tk in tickers[:30]:
                try:
                    cal = yf.Ticker(tk).calendar
                    dates = []
                    if isinstance(cal, dict):
                        dates = cal.get("Earnings Date", []) or []
                    for d in dates[:1]:
                        d_str = str(d)[:10]
                        events.append({
                            "date": d_str, "type": "earnings",
                            "label": f"📈 決算発表【保有】: {tk}",
                            "ticker": tk, "market": "",
                            "importance": "high",   # 保有銘柄の決算は最重要
                        })
                except Exception:
                    continue
        except ImportError:
            pass
    try:
        cache_p.write_text(json.dumps(
            {"fetched": _today().strftime("%Y-%m-%d"), "events": events},
            ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return events


# ==============================================================
# BLOCK 3: ホライズン判定（純粋ロジック・テスト対象）
# ==============================================================

def filter_horizon(events: list[dict], alert_days: int) -> list[dict]:
    """0〜alert_days 日先のイベントを days_until 付きで返す（昇順）。"""
    out = []
    seen = set()
    for e in events:
        du = _days_until(str(e.get("date", "")))
        if du is None or not (0 <= du <= alert_days):
            continue
        key = (e.get("date"), e.get("label"))
        if key in seen:
            continue
        seen.add(key)
        out.append({**e, "days_until": du})
    out.sort(key=lambda x: (x["days_until"],
                            {"high": 0, "medium": 1, "low": 2}
                            .get(x.get("importance", "low"), 2)))
    return out


def build_alerts(upcoming: list[dict]) -> list[dict]:
    """alert_notifier 互換のアラートリストを生成する。"""
    alerts = []
    highs = [e for e in upcoming if e.get("importance") == "high"]
    if not upcoming:
        return alerts
    # 重要イベントは個別アラート
    for e in highs:
        when = "本日" if e["days_until"] == 0 else f"{e['days_until']}日後"
        alerts.append({
            "trigger": "event_horizon",
            "title": f"📅 {when}: {e['label']}",
            "body": f"{e['date']} ({when}) — 事前にポジション・オプション建玉を確認",
            "severity": "high" if e["days_until"] <= 1 else "medium",
        })
    # その他はダイジェスト1本
    others = [e for e in upcoming if e.get("importance") != "high"]
    if others:
        lines = [f"{e['date']} ({e['days_until']}日後) {e['label']}"
                 for e in others[:10]]
        alerts.append({
            "trigger": "event_horizon_digest",
            "title": f"📅 今後{max(e['days_until'] for e in others)}日以内のイベント {len(others)}件",
            "body": "\n".join(lines),
            "severity": "info",
        })
    return alerts


# ==============================================================
# BLOCK 4: エントリポイント
# ==============================================================

def run_event_horizon_scanner(cfg, results: dict = None) -> dict:
    """stock_analyzer_main.py から日次で呼ばれる。"""
    print("\n  📅 イベントホライズン走査中...")
    alert_days = int(getattr(cfg, "EVENT_HORIZON_ALERT_DAYS", 5))

    events = (collect_macro_events(cfg)
              + build_sq_events()
              + collect_earnings_events(cfg))
    upcoming = filter_horizon(events, alert_days)
    alerts = build_alerts(upcoming)

    # Markdown 出力（Obsidian 同期対象）
    try:
        out_dir = Path(getattr(cfg, "INSIGHT_DIR", "stock_data/insights")) / "events"
        out_dir.mkdir(parents=True, exist_ok=True)
        lines = [f"# イベントホライズン（{_today().strftime('%Y-%m-%d')}）", "",
                 f"今後{alert_days}日以内: {len(upcoming)}件", ""]
        for e in upcoming:
            lines.append(f"- **{e['date']}**（{e['days_until']}日後・"
                         f"{e.get('importance','')}）{e['label']}")
        (out_dir / "event_horizon_latest.md").write_text(
            "\n".join(lines), encoding="utf-8")
    except Exception:
        pass

    print(f"    ✅ {alert_days}日以内のイベント: {len(upcoming)}件 "
          f"/ アラート: {len(alerts)}件")
    return {
        "upcoming": upcoming,
        "alerts": alerts,
        "alert_days": alert_days,
        "total_tracked": len(events),
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


if __name__ == "__main__":
    import importlib.util, os
    spec = importlib.util.spec_from_file_location(
        "config", str(Path(__file__).parent / "config.py"))
    cfg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cfg)
    base = Path(os.environ.get("STOCK_ANALYZER_BASE", "")
                or (cfg.DRIVE_BASE if Path(cfg.DRIVE_BASE).exists()
                    else Path(__file__).parent / "stock_data"))
    cfg.CACHE_DIR, cfg.INSIGHT_DIR = base / "cache", base / "insights"
    r = run_event_horizon_scanner(cfg)
    for e in r["upcoming"]:
        print(f"  {e['date']} ({e['days_until']}日後) {e['label']}")
