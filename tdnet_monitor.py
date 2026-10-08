# ==============================================================
# tdnet_monitor.py  v1.0  (M-4 / 村上世彰モデル)
# TDnet 重要適時開示モニター
# ==============================================================
# 【役割】
#   東証 TDnet の開示一覧を解析し、自己株式取得・増配・特別配当等の
#   「株主還元強化シグナル」をリアルタイムで検知する。
#   M-3（アクティビスト候補）と組み合わせることで
#   「触媒発生時の即時アラート」を実現する。
#
# 【取得先】
#   TDnet 開示一覧: https://www.release.tdnet.info/inbs/I_list_001_{YYYYMMDD}.html
#   ※ 公式RSSなし → HTML解析で代替
#
# 【検知カテゴリ】
#   BUYBACK    : 自己株式取得（"自己株式取得" / "自己株式の取得"）
#   CANCEL     : 自己株式消却（株主価値向上）
#   DIV_UP     : 増配・特別配当
#   SPLIT      : 株式分割（需給改善シグナル）
#   MBO        : MBO・TOB（プレミアム期待）
#   MGMT_CHANGE: 役員人事（CEO交代等）
#
# 【出力スキーマ】
#   {
#     "events":       [TDnetEvent, ...],  # 検知した全イベント
#     "buyback":      [TDnetEvent, ...],  # 自己株式取得のみ
#     "div_up":       [TDnetEvent, ...],  # 増配のみ
#     "activist_hits":[TDnetEvent, ...],  # activist_score >= 閾値の銘柄
#     "total":        int,
#     "date":         str,
#     "skipped":      bool,
#     "summary_text": str,
#   }
#
#   TDnetEvent = {
#     "ticker":    str,    # "6758.T" 形式
#     "code":      str,    # "6758" （4桁）
#     "name":      str,    # 会社名
#     "category":  str,    # "BUYBACK" / "DIV_UP" 等
#     "title":     str,    # 開示タイトル
#     "time":      str,    # "09:30" 形式
#     "url":       str,    # 個別開示URL
#   }
#
# 【依存】urllib（標準ライブラリ）/ html.parser / config.py
# 【呼出】from tdnet_monitor import run_tdnet_monitor
# ==============================================================

from __future__ import annotations

import json
import re
import urllib.request
from urllib.parse import urljoin
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional


# ==============================================================
# BLOCK 1: 検知キーワード定義
# ==============================================================

DETECT_PATTERNS = {
    "BUYBACK": [
        "自己株式取得", "自己株式の取得", "自己株式を取得",
        "自己株の取得", "自己株取得",
    ],
    "CANCEL": [
        "自己株式消却", "自己株式の消却", "自己株の消却",
    ],
    "DIV_UP": [
        "増配", "特別配当", "記念配当", "配当予想の修正",
        "配当の修正", "期末配当", "中間配当の修正",
    ],
    "SPLIT": [
        "株式分割", "株式の分割",
    ],
    "MBO": [
        "MBO", "TOB", "株式公開買付",
        "公開買付け", "経営陣による買収",
    ],
    "MGMT_CHANGE": [
        "代表取締役の異動", "取締役の異動", "社長の異動",
        "代表取締役社長交代", "役員人事",
    ],
    "LARGE_HOLDING": [
        "大量保有報告書", "変更報告書（大量保有）",
        "大量保有に関する報告書",
    ],
    "MIDTERM_PLAN": [
        "中期経営計画", "中計", "長期ビジョン", "長期経営計画",
        "経営計画の策定", "中期計画", "新中期経営計画",
    ],
}

# カテゴリの重要度（アラート優先度付けに使用）
CATEGORY_SEVERITY = {
    "BUYBACK":     "high",
    "CANCEL":      "high",
    "DIV_UP":      "high",
    "MBO":         "high",
    "SPLIT":       "medium",
    "MGMT_CHANGE": "medium",
    "LARGE_HOLDING": "high",
    "MIDTERM_PLAN": "medium",
}


# ==============================================================
# BLOCK 2: TDnet HTML パーサー
# ==============================================================

class _TDnetListParser(HTMLParser):
    """
    TDnet 開示一覧ページ（I_list_001_YYYYMMDD.html）を解析する。

    テーブル行から: 時刻 / 会社コード / 会社名 / タイトル / URL を抽出。
    """

    def __init__(self):
        super().__init__()
        self.rows     : list[dict] = []
        self._in_tr   : bool       = False
        self._cells   : list[str]  = []
        self._in_td   : bool       = False
        self._cell_buf: str        = ""
        self._td_link : str        = ""
        self._depth   : int        = 0

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag == "tr":
            self._in_tr  = True
            self._cells  = []
        elif tag == "td" and self._in_tr:
            self._in_td   = True
            self._cell_buf = ""
            self._td_link  = ""
        elif tag == "a" and self._in_td:
            href = attrs_dict.get("href", "")
            # 一覧の表題リンクは "140120260917xxxxxx.pdf" のような相対パス
            if href and not href.startswith(("javascript:", "#")):
                self._td_link = href

    def handle_endtag(self, tag):
        if tag == "td" and self._in_td:
            self._cells.append((self._cell_buf.strip(), self._td_link))
            self._in_td   = False
            self._cell_buf = ""
        elif tag == "tr" and self._in_tr:
            self._in_tr = False
            if len(self._cells) >= 4:
                self.rows.append(self._cells[:])
            self._cells = []

    def handle_data(self, data):
        if self._in_td:
            self._cell_buf += data.strip()


def _fetch_tdnet_page(date_str: str, timeout: int = 15, page: int = 1,
                      quiet: bool = False) -> Optional[str]:
    """
    TDnet の開示一覧ページ HTML を取得する。

    Args:
        date_str : "YYYYMMDD" 形式の日付
        timeout  : タイムアウト秒数

    Returns:
        HTML文字列。取得失敗時は None。
    """
    url = (f"https://www.release.tdnet.info/inbs/"
           f"I_list_{int(page):03d}_{date_str}.html")
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (compatible; StockAnalyzer/1.0; "
                    "+https://github.com/)"
                )
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            charset = "utf-8"
            content_type = resp.headers.get("Content-Type", "")
            if "shift_jis" in content_type.lower() or "sjis" in content_type.lower():
                charset = "shift_jis"
            raw = resp.read()
            return raw.decode(charset, errors="replace")
    except Exception as e:
        if not quiet:
            print(f"    ⚠️  TDnet 取得失敗 ({date_str} p{int(page)}): {str(e)[:60]}")
        return None


def _parse_tdnet_rows(html: str, date_str: str) -> list[dict]:
    """
    HTML からテーブル行を解析して開示一覧を返す。

    Returns:
        [{"time", "code", "name", "title", "url"}, ...]
    """
    parser = _TDnetListParser()
    try:
        parser.feed(html)
    except Exception:
        return []

    items = []
    for cells in parser.rows:
        try:
            # TDnet テーブル構造: [時刻, コード, 会社名, タイトル, ...]
            if len(cells) < 4:
                continue
            time_s  = cells[0][0]  # "09:30"
            code_s  = cells[1][0]  # "6758"
            name_s  = cells[2][0]  # "ソニーグループ"
            title_s = cells[3][0]  # "自己株式取得に関するお知らせ"
            url_s   = cells[3][1]  # URL（あれば）

            # TDnet のコード欄は "67580" / "130A0" の5桁（末尾0）。
            # 4桁数字だけを通す判定にすると全行が捨てられる。
            code_s = code_s.strip().upper()
            if re.match(r"^[0-9][0-9A-Z]{3}0$", code_s):
                code_s = code_s[:4]
            if not re.match(r"^[0-9][0-9A-Z]{3}$", code_s):
                continue

            # 時刻フォーマット検証
            if not re.match(r"^\d{1,2}:\d{2}", time_s.strip()):
                continue

            items.append({
                "time":  time_s.strip(),
                "code":  code_s.strip(),
                "name":  name_s.strip(),
                "title": title_s.strip(),
                "url":   url_s.strip() if url_s else "",
                "date":  date_str,
            })
        except Exception:
            continue

    return items


TDNET_MAX_LIST_PAGES = 15   # 1ページ100件。決算集中日でも1,500件まで


def _tdnet_target_days(now: datetime) -> list:
    """前営業日（土日を飛ばす）と本日の JST 日付を古い順に返す。"""
    jst = timezone(timedelta(hours=9))
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    today = now.astimezone(jst).date()
    prev = today - timedelta(days=1)
    while prev.weekday() >= 5:
        prev -= timedelta(days=1)
    return [prev, today]


def _fetch_tdnet_day_rows(day) -> list:
    """1日分の開示一覧を全ページ読む（100件未満のページで終わり）。"""
    date_str = day.strftime("%Y%m%d")
    rows: list = []
    for page in range(1, TDNET_MAX_LIST_PAGES + 1):
        html = _fetch_tdnet_page(date_str, page=page, quiet=page > 1)
        if html is None:
            break
        page_rows = _parse_tdnet_rows(html, day.strftime("%Y-%m-%d"))
        rows.extend(page_rows)
        if len(page_rows) < 100:
            break
    return rows


# ==============================================================
# BLOCK 3: イベント分類
# ==============================================================

def _classify_event(title: str, cfg=None) -> Optional[str]:
    """
    開示タイトルをキーワードマッチングで分類する。

    監視対象カテゴリは config.TDNET_MONITOR_CATEGORIES で絞り込める。
    未設定・空なら DETECT_PATTERNS の全カテゴリを対象とする。

    Returns:
        カテゴリ文字列 or None（対象外）
    """
    allowed = getattr(cfg, "TDNET_MONITOR_CATEGORIES", None) if cfg else None
    allowed = set(allowed) if allowed else None
    for category, keywords in DETECT_PATTERNS.items():
        if allowed is not None and category not in allowed:
            continue
        for kw in keywords:
            if kw in title:
                return category
    return None


def _build_event(row: dict, category: str) -> dict:
    """
    テーブル行 dict から TDnetEvent 形式に変換する。
    ticker は "{code}.T" 形式に統一。
    """
    url = row["url"]
    if url and not url.startswith("http"):
        # 一覧ページ（/inbs/I_list_...）からの相対パス
        url = urljoin("https://www.release.tdnet.info/inbs/", url)

    return {
        "ticker":   f"{row['code']}.T",
        "code":     row["code"],
        "name":     row["name"],
        "category": category,
        "title":    row["title"],
        "time":     row["time"],
        "date":     row["date"],
        "url":      url,
        "severity": CATEGORY_SEVERITY.get(category, "info"),
    }


# ==============================================================
# BLOCK 4: activist_score との照合
# ==============================================================

def _find_activist_hits(events: list[dict], cfg) -> list[dict]:
    """
    検知したイベントと screener の activist_top（M-3）を照合する。
    activist_score >= 閾値 かつ TDnet に触媒が出た銘柄を返す。

    screener_history.json の直近エントリから activist_score を参照。
    """
    threshold = getattr(cfg, "ACTIVIST_SCORE_THRESHOLD", 70.0)
    # BLOCK M-3 の ENABLE_ACTIVIST_FILTER（機能そのものの有効フラグ）。
    # tests/test_enable_flags_wiring.py が配線を検査する。
    if not getattr(cfg, "ENABLE_ACTIVIST_FILTER", True):
        return []
    if not getattr(cfg, "ACTIVIST_CATALYST_ALERT", True):
        return []

    # screener_history から activist_score を取得
    hist_path = Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / "screener_history.json"
    activist_tickers: dict[str, float] = {}

    if hist_path.exists():
        try:
            history = json.loads(hist_path.read_text(encoding="utf-8"))
            # 直近7日分の最新エントリを参照
            for date_key in sorted(history.keys(), reverse=True)[:7]:
                for c in history[date_key]:
                    ticker = c.get("ticker", "")
                    score  = c.get("activist_score")
                    if ticker and score is not None and float(score) >= threshold:
                        if ticker not in activist_tickers:
                            activist_tickers[ticker] = float(score)
        except Exception:
            pass

    if not activist_tickers:
        return []

    hits = []
    for ev in events:
        if ev["ticker"] in activist_tickers:
            ev_with_score = {
                **ev,
                "activist_score": activist_tickers[ev["ticker"]],
            }
            hits.append(ev_with_score)

    return hits


# ==============================================================
# BLOCK 5: キャッシュ管理（重複通知防止）
# ==============================================================

def _load_notified_cache(cfg) -> set:
    """
    既に通知済みのイベントIDセットを読み込む。
    形式: {"6758.T_2026-03-29_BUYBACK", ...}
    """
    path = Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / "tdnet_notified.json"
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return set(data.get("notified", []))
        except Exception:
            pass
    return set()


def _save_notified_cache(cfg, notified: set) -> None:
    """通知済みキャッシュを保存する（30日分保持）。"""
    path = Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / "tdnet_notified.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 30日より古いエントリを削除
        cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        pruned = {
            nid for nid in notified
            if len(nid.split("_")) >= 2 and nid.split("_")[1] >= cutoff
        }
        path.write_text(
            json.dumps({"notified": list(pruned)}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"    ⚠️  tdnet_notified.json 保存失敗: {e}")


def _make_event_id(event: dict) -> str:
    return f"{event['ticker']}_{event['date']}_{event['category']}"


# ==============================================================
# BLOCK 6: サマリーテキスト生成
# ==============================================================

def _format_tdnet_summary(events: list[dict], activist_hits: list[dict]) -> str:
    """weekly_report / alert 向け Markdown テキストを生成する。"""
    if not events:
        return ""

    ICONS = {
        "BUYBACK":     "💴",
        "CANCEL":      "🔥",
        "DIV_UP":      "💰",
        "SPLIT":       "✂️",
        "MBO":         "🎯",
        "MGMT_CHANGE": "👤",
        "LARGE_HOLDING": "🚨",
    }

    lines = ["## 📢 TDnet 重要開示（本日）", ""]

    # カテゴリ別に集計して表示
    from collections import defaultdict
    by_cat: dict[str, list] = defaultdict(list)
    for ev in events:
        by_cat[ev["category"]].append(ev)

    for cat, cat_events in sorted(by_cat.items()):
        icon = ICONS.get(cat, "📄")
        label_map = {
            "BUYBACK": "自己株式取得",
            "CANCEL":  "自己株式消却",
            "DIV_UP":  "増配・特別配当",
            "SPLIT":   "株式分割",
            "MBO":     "MBO/TOB",
            "MGMT_CHANGE": "役員異動",
        }
        lines.append(f"### {icon} {label_map.get(cat, cat)} ({len(cat_events)}件)")
        for ev in cat_events[:5]:
            lines.append(f"- **{ev['name']}**（{ev['ticker']}）{ev['time']} — {ev['title'][:40]}")
        lines.append("")

    # アクティビスト候補との照合結果
    if activist_hits:
        lines += ["### 🎯 触媒発生！アクティビスト候補銘柄", ""]
        for ev in activist_hits:
            lines.append(
                f"⚡ **{ev['name']}**（{ev['ticker']}）"
                f"activist_score={ev.get('activist_score',0):.0f}点 "
                f"| {ev['title'][:40]}"
            )
        lines.append("")

    return "\n".join(lines)


# ==============================================================
# BLOCK 7: メインエントリポイント
# ==============================================================

def run_tdnet_monitor(cfg, results: dict = None) -> dict:
    """
    TDnet の本日開示一覧を解析し、株主還元シグナルを検知する。

    stock_analyzer_main.py から以下のように呼び出す:
        _tdnet = _load_local_module("tdnet_monitor", ...)
        if _tdnet:
            tdnet_result = _tdnet.run_tdnet_monitor(cfg, results)
            results["tdnet"] = tdnet_result

    Returns:
        上記スキーマの dict
    """
    if not getattr(cfg, "ENABLE_TDNET_MONITOR", True):
        return {"skipped": True, "events": [], "total": 0}

    print("\n  📢 TDnetモニター 開始...")
    t0       = datetime.now()

    # ── Step 1-2: HTML 取得・行解析 ──────────────────────────
    # 実行は JST 9〜14時台で、決算など大半の開示は前営業日の15時以降に出る。
    # 100件を超える日は2ページ目以降もあるため、前営業日と本日の全ページを読む
    # （通知済みキャッシュで重複は除外される）。日付は JST で決める。
    rows: list[dict] = []
    days = _tdnet_target_days(datetime.now(timezone.utc))
    for day in days:
        rows.extend(_fetch_tdnet_day_rows(day))
    today_s = days[-1].strftime("%Y-%m-%d")
    if not rows:
        print("    ⚠️  TDnet HTML 取得失敗または開示0件 → スキップ")
        return {"skipped": True, "events": [], "total": 0,
                "timestamp": t0.strftime("%Y-%m-%d %H:%M:%S")}
    print(f"    解析行数: {len(rows)}件 （{' / '.join(d.strftime('%m-%d') for d in days)}）")

    # ── Step 3: イベント分類 ────────────────────────────────
    all_events: list[dict] = []
    for row in rows:
        cat = _classify_event(row["title"], cfg)
        if cat:
            all_events.append(_build_event(row, cat))

    # ── Step 4: 重複通知防止（キャッシュ照合）──────────────
    notified      = _load_notified_cache(cfg)
    new_events    = [ev for ev in all_events
                     if _make_event_id(ev) not in notified]
    new_notified  = notified | {_make_event_id(ev) for ev in new_events}
    _save_notified_cache(cfg, new_notified)

    # ── Step 5: activist_score との照合 ─────────────────────
    activist_hits = _find_activist_hits(new_events, cfg)

    # ── Step 6: カテゴリ別分類 ──────────────────────────────
    buyback = [ev for ev in new_events if ev["category"] == "BUYBACK"]
    div_up  = [ev for ev in new_events if ev["category"] == "DIV_UP"]
    cancel  = [ev for ev in new_events if ev["category"] == "CANCEL"]
    mbo     = [ev for ev in new_events if ev["category"] == "MBO"]
    large_holding = [ev for ev in new_events if ev["category"] == "LARGE_HOLDING"]

    elapsed = (datetime.now() - t0).seconds
    print(
        f"    ✅ TDnet完了（{elapsed}秒）: "
        f"総検知{len(all_events)}件 | "
        f"新規{len(new_events)}件 | "
        f"自己株買い{len(buyback)} / 増配{len(div_up)} / "
        f"アクティビスト照合{len(activist_hits)}件"
    )
    if activist_hits:
        for hit in activist_hits[:3]:
            print(f"      ⚡ {hit['name']}({hit['ticker']}): "
                  f"activist={hit.get('activist_score',0):.0f}pt "
                  f"→ {hit['title'][:30]}")

    summary_text = _format_tdnet_summary(new_events, activist_hits)

    return {
        "events":         new_events,
        "all_events":     all_events,   # 重複含む全件（参照用）
        "buyback":        buyback,
        "div_up":         div_up,
        "cancel":         cancel,
        "mbo":            mbo,
        "large_holding":  large_holding,    # P-4
        "midterm_plan":   [ev for ev in new_events if ev["category"] == "MIDTERM_PLAN"],  # I-5
        "activist_hits":  activist_hits,
        "total":          len(new_events),
        "date":           today_s,
        "summary_text":   summary_text,
        "skipped":        False,
        "timestamp":      t0.strftime("%Y-%m-%d %H:%M:%S"),
    }
