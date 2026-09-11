# ==============================================================
# policy_radar.py  v1.0  (Wave 3 / Wave 4 - Idea3-P2)
# 政府答弁・官僚PDF先回り予測システム
# ==============================================================
# 【役割】
#   官庁RSS・国会議事録API・外交プレスリリースを横断的に監視し、
#   政策シグナルを業種スコアに先回り反映する。
#
# 【実装フェーズ】
#   P1（日次）: 官庁RSS + 国会議事録API でキーワード検出
#   P2（週次）: PDF テキスト抽出（テキスト型PDFのみ）
#   P3（週次）: 実現度スコア + イベントカレンダー統合 + composite_score 反映
#
# 【出力スキーマ（run_policy_radar の返り値）】
#   {
#     "total_signals":   int,       # 検出シグナル総数
#     "leading_signals": [...],     # LEADING_INDICATOR_RULES 互換シグナルリスト
#     "events_30d":      [...],     # 今後30日以内の政策イベントリスト
#     "sector_boosts":   {...},     # {sector_code: adj_pt}
#     "items":           [...],     # 検出アイテム詳細リスト
#     "cumulative_hits": {...},     # 累積スコア（複数ソース裏付け用）
#     "cached":          bool,      # キャッシュ利用フラグ
#     "timestamp":       str,
#   }
#
# 【leading_signals 1件のスキーマ】
#   {
#     "rule_id":     str,     # "policy_radar_半導体補助金"
#     "label":       str,
#     "lag_weeks":   int,
#     "signal":      {"tailwind": [...], "headwind": [...]},
#     "rationale":   {sector_code: str, ...},
#     "triggered_val": float,  # 実現度スコア
#   }
#
# 【キャッシュ】
#   CACHE_DIR/policy_radar_cache.json  TTL: POLICY_RADAR_INTENT_CACHE_TTL_DAYS（30日）
#   CACHE_DIR/pdf_url_cache.json       TTL: PDF_URL_CACHE_TTL_DAYS (30日)
#   CACHE_DIR/policy_radar_cumulative.json  累積スコア（恒久保持・上位件で削除）
#
# 【Wave 4 - Idea3-P2 連携】
#   3ソース以上の裏付けがあると policy_short_cache から
#   policy_medium_cache（L3）へ自動昇格する累積スコア判定ロジックを含む。
#
# 【依存】
#   config.py (BLOCK policy_radar / BLOCK 35)
#   policy_signal_applier.py (register_signals / load_policy_short_cache)
#   feedparser / requests / pdfplumber（オプション）/ PyMuPDF（オプション）
#
# 【呼び出し（stock_analyzer_main.py 既存コードと互換）】
#   from policy_radar import run_policy_radar
#   pr_result = run_policy_radar(cfg, results)
# ==============================================================

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# ==============================================================
# BLOCK 0: デフォルト定数（config.py 未設定時のフォールバック）
# ==============================================================

# 実現度スコア（ソース × 役職ウェイト）
# $realization\_score = source\_weight \times time\_decay$

from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'PR_SOURCE_WEIGHTS': '_SOURCE_WEIGHTS',
    'PR_CUMULATIVE_THRESHOLD_FOR_L3': '_CUMULATIVE_THRESHOLD_FOR_L3',
}

_SOURCE_WEIGHTS: dict = {
    "閣議決定":       1.00,
    "内閣決定":       1.00,
    "首相":           0.90,
    "総理":           0.90,
    "経済対策":       0.85,
    "大臣":           0.70,
    "副大臣":         0.60,
    "パブリックコメント": 0.60,
    "パブコメ":       0.60,
    "中間報告":       0.40,
    "審議会":         0.40,
    "諮問":           0.35,
    "検討":           0.25,
    "研究会":         0.20,
    "議員立法":       0.30,
    "委員会":         0.35,
}

# 時間軸キーワード → 実現ラグ（週）
_TIME_KEYWORDS: dict = {
    "来年度予算":      36,   # 約9ヶ月
    "来年度":          36,
    "次年度":          36,
    "法改正を行う":    14,   # 約3.5ヶ月
    "法改正":          20,
    "法律化":          16,
    "施行":            8,
    "今年度中":        12,
    "年内":            12,
    "早急に":          4,
    "速やかに":        4,
    "直ちに":          2,
    "検討する":        52,   # 長期不確実
    "検討を進める":    36,
    "令和7年":         26,
    "令和8年":         52,
}

# デフォルト政策キーワードマップ
# {keyword: {"sectors": [...sector_code], "direction": "tailwind"/"headwind",
#            "label": str, "base_score": float, "lag_weeks": int}}
_DEFAULT_KEYWORD_MAP: dict = {
    "半導体": {
        "sectors":    ["1016", "1025"],
        "direction":  "tailwind",
        "label":      "半導体政策",
        "base_score": 5.0,
        "lag_weeks":  8,
    },
    "半導体補助金": {
        "sectors":    ["1016", "1025"],
        "direction":  "tailwind",
        "label":      "半導体補助金",
        "base_score": 7.0,
        "lag_weeks":  8,
    },
    "防衛費": {
        "sectors":    ["1015", "1016", "1018"],
        "direction":  "tailwind",
        "label":      "防衛費増額",
        "base_score": 5.0,
        "lag_weeks":  12,
    },
    "防衛装備": {
        "sectors":    ["1015", "1016", "1018"],
        "direction":  "tailwind",
        "label":      "防衛装備調達",
        "base_score": 4.0,
        "lag_weeks":  16,
    },
    "GX": {
        "sectors":    ["1020", "1009", "1016"],
        "direction":  "tailwind",
        "label":      "グリーントランスフォーメーション",
        "base_score": 4.0,
        "lag_weeks":  20,
    },
    "グリーントランスフォーメーション": {
        "sectors":    ["1020", "1009", "1016"],
        "direction":  "tailwind",
        "label":      "GX政策",
        "base_score": 4.0,
        "lag_weeks":  20,
    },
    "再生可能エネルギー": {
        "sectors":    ["1020", "1009"],
        "direction":  "tailwind",
        "label":      "再エネ推進",
        "base_score": 3.0,
        "lag_weeks":  24,
    },
    "子育て支援": {
        "sectors":    ["1004", "1027", "1033"],
        "direction":  "tailwind",
        "label":      "子育て支援政策",
        "base_score": 3.0,
        "lag_weeks":  12,
    },
    "インバウンド": {
        "sectors":    ["1033", "1021", "1022"],
        "direction":  "tailwind",
        "label":      "インバウンド促進",
        "base_score": 3.0,
        "lag_weeks":  8,
    },
    "医療DX": {
        "sectors":    ["1008", "1025"],
        "direction":  "tailwind",
        "label":      "医療DX推進",
        "base_score": 4.0,
        "lag_weeks":  16,
    },
    "デジタル": {
        "sectors":    ["1025"],
        "direction":  "tailwind",
        "label":      "デジタル化推進",
        "base_score": 3.0,
        "lag_weeks":  12,
    },
    "インフラ": {
        "sectors":    ["1003", "1014", "1021"],
        "direction":  "tailwind",
        "label":      "インフラ投資",
        "base_score": 4.0,
        "lag_weeks":  16,
    },
    "規制緩和": {
        "sectors":    ["1025", "1033", "1027"],
        "direction":  "tailwind",
        "label":      "規制緩和",
        "base_score": 3.0,
        "lag_weeks":  24,
    },
    "金融規制": {
        "sectors":    ["1028", "1029", "1030"],
        "direction":  "headwind",
        "label":      "金融規制強化",
        "base_score": 3.0,
        "lag_weeks":  20,
    },
    "増税": {
        "sectors":    ["1027", "1033", "1004"],
        "direction":  "headwind",
        "label":      "増税リスク",
        "base_score": 3.0,
        "lag_weeks":  16,
    },
    "関税": {
        "sectors":    ["1017", "1016", "1026"],
        "direction":  "headwind",
        "label":      "関税リスク",
        "base_score": 4.0,
        "lag_weeks":  8,
    },
    "賃上げ": {
        "sectors":    ["1027", "1033", "1003"],
        "direction":  "headwind",
        "label":      "賃上げコスト増",
        "base_score": 2.0,
        "lag_weeks":  4,
    },
}

# デフォルト官庁RSSフィード
_DEFAULT_RSS_FEEDS: list[dict] = [
    {
        "name":     "経産省プレスリリース",
        "url":      "https://www.meti.go.jp/press/atom.xml",
        "priority": "high",
        "has_pdf":  True,
    },
    {
        "name":     "金融庁",
        "url":      "https://www.fsa.go.jp/news/rss/news.rdf",
        "priority": "high",
        "has_pdf":  False,
    },
    {
        "name":     "デジタル庁",
        "url":      "https://www.digital.go.jp/feed",
        "priority": "medium",
        "has_pdf":  False,
    },
    {
        "name":     "首相官邸",
        "url":      "https://www.kantei.go.jp/jp/headline/rss/index.xml",
        "priority": "high",
        "has_pdf":  False,
    },
    {
        "name":     "外務省プレスリリース",
        "url":      "https://www.mofa.go.jp/press/release/rss.xml",
        "priority": "medium",
        "has_pdf":  False,
    },
    {
        "name":     "内閣府",
        "url":      "https://www.cao.go.jp/rss/news_all.xml",
        "priority": "medium",
        "has_pdf":  True,
    },
    {
        "name":     "AMED医療研究開発機構",
        "url":      "https://www.amed.go.jp/rss/news.xml",
        "priority": "low",
        "has_pdf":  False,
    },
    {
        "name":     "e-Gov パブコメ",
        "url":      "https://search.e-gov.go.jp/servlet/Public?CLASSNAME=PCMMSTRECENTLY&id=0&Mode=0",
        "priority": "medium",
        "has_pdf":  False,
        "scrape":   True,   # RSS未対応→スクレイプ
    },
]

# 累積スコアが閾値以上で policy_medium_cache (L3) に自動昇格（Wave 4 Idea3-P2）
_CUMULATIVE_THRESHOLD_FOR_L3 = 3   # 3ソース以上の裏付けで L3 昇格


# ==============================================================
# BLOCK 1: キャッシュ管理
# ==============================================================

def _cache_dir(cfg) -> Path:
    return Path(getattr(cfg, "CACHE_DIR", "stock_data/cache"))


def _load_radar_cache(cfg) -> Optional[dict]:
    """TTL（デフォルト30日）以内のキャッシュがあれば返す。週次更新対応。"""
    p = _cache_dir(cfg) / "policy_radar_cache.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        ts = datetime.fromisoformat(data.get("timestamp", "2000-01-01"))
        ttl_days = getattr(cfg, "POLICY_RADAR_INTENT_CACHE_TTL_DAYS", 30) if cfg else 30
        if datetime.now() - ts < timedelta(days=ttl_days):
            return data
    except Exception:
        pass
    return None


def _save_radar_cache(cfg, payload: dict) -> None:
    p = _cache_dir(cfg) / "policy_radar_cache.json"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    except Exception as e:
        print(f"    ⚠️  [policy_radar] キャッシュ保存失敗: {e}")


def _load_cumulative(cfg) -> dict:
    """累積スコアを読み込む。{keyword: {"count": int, "sectors": [...], ...}}"""
    p = _cache_dir(cfg) / "policy_radar_cumulative.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cumulative(cfg, data: dict) -> None:
    p = _cache_dir(cfg) / "policy_radar_cumulative.json"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        # 上限 500件（古いエントリを削除）
        if len(data) > 500:
            # last_seen の古い順に削除
            sorted_keys = sorted(
                data.keys(),
                key=lambda k: data[k].get("last_seen", "2000-01-01"),
            )
            for old_key in sorted_keys[:len(data) - 500]:
                del data[old_key]
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    except Exception as e:
        print(f"    ⚠️  [policy_radar] 累積スコア保存失敗: {e}")


def _load_pdf_url_cache(cfg) -> dict:
    """取得済み PDF URL のキャッシュ（TTL: PDF_URL_CACHE_TTL_DAYS）"""
    p = _cache_dir(cfg) / "pdf_url_cache.json"
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        ttl  = getattr(cfg, "PDF_URL_CACHE_TTL_DAYS", 30)
        now  = datetime.now()
        cleaned = {
            url: meta for url, meta in data.items()
            if (now - datetime.fromisoformat(
                meta.get("fetched_at", "2000-01-01")
            )).days <= ttl
        }
        return cleaned
    except Exception:
        return {}


def _save_pdf_url_cache(cfg, data: dict) -> None:
    p = _cache_dir(cfg) / "pdf_url_cache.json"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    except Exception:
        pass


# ==============================================================
# BLOCK 2: 情報源クローラー
# ==============================================================

def _fetch_rss_items(feeds: list[dict], max_feeds: int, timeout: int = 10) -> list[dict]:
    """
    RSS フィードを一括取得してアイテムリストを返す。

    Returns:
        [{"title": str, "summary": str, "link": str,
          "published": str, "source_name": str,
          "priority": str, "has_pdf": bool}, ...]
    """
    items = []
    try:
        import feedparser
    except ImportError:
        print("    ⚠️  [policy_radar] feedparser 未インストール → RSS スキップ")
        return items

    import urllib.request

    fetched = 0
    for feed_info in feeds[:max_feeds]:
        if feed_info.get("scrape"):
            continue   # スクレイプ対応は別関数で処理（P2以降）
        url = feed_info.get("url", "")
        if not url:
            continue
        try:
            # ★ feedparser.parse(url) は timeout を一切適用せず、応答停止サーバで
            #   無限ハングする（実害: 2026-06-25 本番が官庁RSSで約2h23mハングし、
            #   180分ジョブtimeoutで全体強制終了＝当日解析が未完）。
            #   必ず urllib で timeout 付き取得し、取得済みバイト列を渡す
            #   （_fetch_kokkai_items / _fetch_pdf と同じ流儀）。
            req = urllib.request.Request(
                url, headers={"User-Agent": "stock_analyzer/1.0 (policy_radar)"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            parsed = feedparser.parse(raw)
            for entry in parsed.entries[:15]:   # 1フィードあたり最大15件
                title   = getattr(entry, "title",   "") or ""
                summary = getattr(entry, "summary", "") or ""
                link    = getattr(entry, "link",    "") or ""
                pub     = getattr(entry, "published", "") or ""
                items.append({
                    "title":       title,
                    "summary":     summary[:500],   # 500文字でトリム
                    "link":        link,
                    "published":   pub,
                    "source_name": feed_info["name"],
                    "priority":    feed_info.get("priority", "medium"),
                    "has_pdf":     feed_info.get("has_pdf", False),
                    "text":        f"{title} {summary}",
                })
            fetched += 1
        except Exception as e:
            print(f"    ⚠️  [policy_radar] RSS取得失敗 ({feed_info['name']}): "
                  f"{str(e)[:50]}")
        time.sleep(0.5)   # 官庁サーバへの負荷軽減

    print(f"    📡 [policy_radar] RSS: {fetched}フィード取得 / {len(items)}件")
    return items


def _fetch_kokkai_items(keywords: list[str], timeout: int = 10) -> list[dict]:
    """
    国会会議録検索API（NDL）からキーワード検索してアイテムを返す。
    URL: https://kokkai.ndl.go.jp/api/1.0/speech
    APIキー不要・無料・商用利用可。

    Returns:
        [{"title": str, "summary": str, "link": str,
          "source_name": "国会議事録", ...}, ...]
    """
    items = []
    base_url = "https://kokkai.ndl.go.jp/api/1.0/speech"

    for kw in keywords[:5]:   # 上限5キーワード（API負荷対策）
        try:
            import urllib.request, urllib.parse
            params = urllib.parse.urlencode({
                "any":         kw,
                "maximumRecords": 5,
                "recordPacking": "json",
                "from": (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d"),
            })
            url = f"{base_url}?{params}"
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "stock_analyzer/1.0 (policy_radar)"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            records = data.get("speechRecord", []) or []
            for rec in records:
                speech_text = str(rec.get("speech", ""))[:600]
                items.append({
                    "title":       f"[国会] {kw}: {rec.get('speakerPosition','')}"
                                   f" {rec.get('speaker','')}",
                    "summary":     speech_text,
                    "link":        rec.get("speechURL", ""),
                    "published":   rec.get("date", ""),
                    "source_name": "国会議事録",
                    "priority":    "high",
                    "has_pdf":     False,
                    "text":        f"{kw} {speech_text}",
                    "speaker_pos": rec.get("speakerPosition", ""),
                    "speaker":     rec.get("speaker", ""),
                    "meeting":     rec.get("nameOfMeeting", ""),
                })
            time.sleep(1.0)   # NDL API レート制限対策

        except Exception as e:
            print(f"    ⚠️  [policy_radar] 国会API取得失敗 ({kw}): "
                  f"{str(e)[:50]}")

    print(f"    📜 [policy_radar] 国会議事録: {len(items)}件")
    return items


# ==============================================================
# BLOCK 3: PDF テキスト抽出（P2・週次実行）
# ==============================================================

def _extract_pdf_text(url: str, pdf_cache: dict, timeout: int = 15) -> str:
    """
    PDF URL からテキストを抽出する。
    pdfplumber（テキスト型PDF）→ PyMuPDF fitz（フォールバック）の順で試みる。
    キャッシュ済みURLは再取得しない。

    Returns:
        str: 抽出テキスト（最大2000文字）。失敗時は ""。
    """
    if url in pdf_cache:
        return pdf_cache[url].get("text", "")

    text = ""
    try:
        import urllib.request, io
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "stock_analyzer/1.0"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            pdf_bytes = resp.read()

        # ── pdfplumber で試みる ───────────────────────────
        try:
            import pdfplumber
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                pages_text = []
                for page in pdf.pages[:5]:   # 先頭5ページのみ
                    t = page.extract_text()
                    if t:
                        pages_text.append(t)
                text = " ".join(pages_text)[:2000]
        except Exception:
            pass

        # ── PyMuPDF (fitz) フォールバック ─────────────────
        if not text:
            try:
                import fitz   # PyMuPDF
                doc = fitz.open(stream=pdf_bytes, filetype="pdf")
                pages_text = []
                for i, page in enumerate(doc):
                    if i >= 5:
                        break
                    pages_text.append(page.get_text())
                doc.close()
                text = " ".join(pages_text)[:2000]
            except Exception:
                pass

    except Exception as e:
        print(f"    ⚠️  [policy_radar] PDF取得失敗 ({url[:60]}): "
              f"{str(e)[:40]}")

    # キャッシュに記録（失敗時も記録して再試行を防ぐ）
    pdf_cache[url] = {
        "text":       text,
        "fetched_at": datetime.now().isoformat(),
    }
    return text


def _find_pdf_links_in_item(item: dict) -> list[str]:
    """
    RSS アイテムの link / summary から PDF URL を抽出する。
    .pdf で終わるURLのみ対象。
    """
    import re
    text  = f"{item.get('link','')} {item.get('summary','')}"
    pdfs  = re.findall(r'https?://[^\s\'"<>]+\.pdf', text, re.IGNORECASE)
    return list(set(pdfs))[:3]   # 1アイテムあたり最大3件


# ==============================================================
# BLOCK 4: キーワード × セクターマッピング・実現度スコアリング
# ==============================================================

def _get_keyword_map(cfg) -> dict:
    """config.POLICY_KEYWORD_MAP が空なら _DEFAULT_KEYWORD_MAP を返す。"""
    km = getattr(cfg, "POLICY_KEYWORD_MAP", {})
    return km if km else _DEFAULT_KEYWORD_MAP


def _calc_realization_score(text: str, source_name: str) -> float:
    """
    テキストから実現度スコアを計算する（0.0〜1.0）。

    $score = max(source\\_weight\\_i)$ で最も権威あるキーワードを採用。
    複数のソースキーワードが出現する場合は最大値を取る。
    """
    max_weight = 0.0
    for keyword, weight in _SOURCE_WEIGHTS.items():
        if keyword in text:
            max_weight = max(max_weight, weight)

    # 国会議事録の場合は speaker_pos も参照（BLOCKで設定済み）
    if "国会議事録" in source_name:
        # デフォルト: 議員発言 = 0.30
        if max_weight == 0.0:
            max_weight = 0.30

    return round(max_weight, 3)


def _calc_lag_weeks(text: str, kw_meta: dict) -> int:
    """
    テキストの時間軸キーワードから実現ラグ（週）を推定する。
    複数マッチの場合は最短ラグを採用（保守的推定）。
    """
    default_lag = kw_meta.get("lag_weeks", 16)
    lags = []
    for time_kw, weeks in _TIME_KEYWORDS.items():
        if time_kw in text:
            lags.append(weeks)
    return min(lags) if lags else default_lag

# ==============================================================
# BLOCK 4-B: FinBERT 文意3分類（Wave 5 - Idea3-P3）
# ==============================================================

def _classify_intent_with_finbert(text: str, cfg) -> dict:
    """
    テキストの文意を FinBERT で「確定/検討中/否決」の3クラスに分類する。

    判定ロジック:
      1. ルールベース事前フィルタ（高速）
         - 「閣議決定」「施行」等の確定表現 → 即「確定」を返す
         - 「見送り」「撤回」「否決」等の否決表現 → 即「否決」を返す
      2. FinBERT 感情スコア（精度向上）
         - POSITIVE $\\geq$ POLICY_INTENT_CONFIRMED_THRESHOLD → 「確定」
         - NEGATIVE $\\geq$ POLICY_INTENT_REJECTED_THRESHOLD  → 「否決」
         - それ以外 → 「検討中」
      3. FinBERT 未使用時（POLICY_RADAR_ENABLE_FINBERT_INTENT=False
         またはモデルロード失敗） → ルールベース結果のみ返す

    Args:
        text: 判定対象テキスト（タイトル + サマリーの連結を想定）
        cfg:  config モジュール

    Returns:
        {
          "intent":       "confirmed" | "considering" | "rejected",
          "intent_label": "確定" | "検討中" | "否決",
          "multiplier":   float,   # POLICY_INTENT_MULTIPLIERS の値
          "method":       "rule" | "finbert",
          "confidence":   float,   # 0.0〜1.0（ルールベース時は1.0固定）
        }
    """
    # デフォルト（検討中）
    _default = {
        "intent":       "considering",
        "intent_label": "検討中",
        "multiplier":   0.50,
        "method":       "rule",
        "confidence":   0.50,
    }

    labels      = getattr(cfg, "POLICY_INTENT_LABELS",
                          {"confirmed": "確定", "considering": "検討中", "rejected": "否決"})
    multipliers = getattr(cfg, "POLICY_INTENT_MULTIPLIERS",
                          {"confirmed": 1.00, "considering": 0.50, "rejected": 0.00})

    # ── Step 1: ルールベース事前フィルタ ──────────────────────
    _CONFIRMED_PATTERNS = [
        "閣議決定", "閣議了解", "法律が成立", "施行", "公布",
        "予算に計上", "正式決定", "決定した", "決定しました",
        "実施する", "実施します", "実施することとした",
        "開始する", "開始します", "導入する", "導入します",
    ]
    _REJECTED_PATTERNS = [
        "見送り", "撤回", "廃案", "否決", "反対多数",
        "断念", "中止", "凍結", "不採択", "見直しを余儀なく",
    ]

    for pat in _CONFIRMED_PATTERNS:
        if pat in text:
            intent = "confirmed"
            return {
                "intent":       intent,
                "intent_label": labels.get(intent, "確定"),
                "multiplier":   multipliers.get(intent, 1.00),
                "method":       "rule",
                "confidence":   1.00,
            }

    for pat in _REJECTED_PATTERNS:
        if pat in text:
            intent = "rejected"
            return {
                "intent":       intent,
                "intent_label": labels.get(intent, "否決"),
                "multiplier":   multipliers.get(intent, 0.00),
                "method":       "rule",
                "confidence":   1.00,
            }

    # ── Step 2: FinBERT 感情スコアによる精度向上 ──────────────
    if not getattr(cfg, "POLICY_RADAR_ENABLE_FINBERT_INTENT", True):
        return _default

    try:
        import sys as _sys, importlib as _imp, pathlib as _pl
        _sys.path.insert(0, str(_pl.Path(__file__).parent))
        _fa  = _imp.import_module("finbert_analyzer")
        _nlp = _fa._get_or_init_pipeline()

        truncated = text[:512]
        result    = _nlp(truncated)[0]
        raw_label = result.get("label", "NEUTRAL").upper()
        raw_score = float(result.get("score", 0.5))

        confirmed_thresh = getattr(cfg, "POLICY_INTENT_CONFIRMED_THRESHOLD", 0.70)
        rejected_thresh  = getattr(cfg, "POLICY_INTENT_REJECTED_THRESHOLD",  0.65)

        if raw_label == "POSITIVE" and raw_score >= confirmed_thresh:
            intent = "confirmed"
        elif raw_label == "NEGATIVE" and raw_score >= rejected_thresh:
            intent = "rejected"
        else:
            intent = "considering"

        return {
            "intent":       intent,
            "intent_label": labels.get(intent, "検討中"),
            "multiplier":   multipliers.get(intent, 0.50),
            "method":       "finbert",
            "confidence":   round(raw_score, 4),
        }

    except Exception as _e:
        print(f"    ⚠️  [policy_radar] FinBERT文意分類失敗（ルールベースで継続）: "
              f"{str(_e)[:60]}")
        return _default

def _score_items(
    items:       list[dict],
    keyword_map: dict,
    cumulative:  dict,
    cfg=None,
) -> tuple[list[dict], dict]:
    """
    全アイテムをキーワードマッチしてスコアリングする。

    Returns:
        (scored_items, updated_cumulative)
        scored_items: [{
          "keyword":           str,
          "kw_meta":           dict,
          "source_name":       str,
          "realization_score": float,
          "lag_weeks":         int,
          "title":             str,
          "link":              str,
          "published":         str,
          "hit_count":         int,   # cumulative の何回目か
        }, ...]
    """
    scored = []
    today  = datetime.now().strftime("%Y-%m-%d")

    for item in items:
        text = item.get("text", "")
        if not text:
            continue

        for keyword, kw_meta in keyword_map.items():
            if keyword not in text:
                continue

            r_score = _calc_realization_score(text, item.get("source_name", ""))
            if r_score == 0.0:
                r_score = 0.20   # キーワードにヒットした時点でデフォルト

            lag = _calc_lag_weeks(text, kw_meta)

            # 累積スコア更新
            cum_key = f"{keyword}_{kw_meta.get('label','')}"
            if cum_key not in cumulative:
                cumulative[cum_key] = {
                    "keyword":    keyword,
                    "label":      kw_meta.get("label", keyword),
                    "sectors":    kw_meta.get("sectors", []),
                    "direction":  kw_meta.get("direction", "tailwind"),
                    "count":      0,
                    "total_score": 0.0,
                    "last_seen":  today,
                    "lag_weeks":  lag,
                }
            cumulative[cum_key]["count"]       += 1
            cumulative[cum_key]["total_score"] += r_score
            cumulative[cum_key]["last_seen"]    = today
            cumulative[cum_key]["lag_weeks"]    = min(
                cumulative[cum_key]["lag_weeks"], lag
            )

            # ── Wave 5 - Idea3-P3: FinBERT文意3分類 ──────────────
            # try/except 保護: 分類失敗時は multiplier=0.5（検討中）で継続
            _intent_result = {"intent": "considering", "intent_label": "検討中",
                              "multiplier": 0.50, "method": "rule", "confidence": 0.50}
            try:
                _intent_result = _classify_intent_with_finbert(text, cfg if 'cfg' in dir() else type('_C', (), {})())
            except Exception:
                pass

            # 実現度スコアに文意乗数を適用
            # $adjusted\_score = r\_score \times intent\_multiplier$
            _adjusted_score = round(r_score * _intent_result["multiplier"], 4)

            scored.append({
                "keyword":              keyword,
                "kw_meta":              kw_meta,
                "source_name":          item.get("source_name", ""),
                "realization_score":    _adjusted_score,   # 文意補正後スコア
                "realization_score_raw": r_score,          # 補正前生スコア
                "lag_weeks":            lag,
                "title":                item.get("title", "")[:100],
                "link":                 item.get("link", ""),
                "published":            item.get("published", ""),
                "hit_count":            cumulative[cum_key]["count"],
                "speaker_pos":          item.get("speaker_pos", ""),
                "intent_classification": _intent_result,   # Wave 5 - Idea3-P3
            })

    # 重複排除（同キーワード × 同ソース の最高スコアのみ保持）
    deduped: dict = {}
    for s in scored:
        key = f"{s['keyword']}_{s['source_name']}"
        if key not in deduped or s["realization_score"] > deduped[key]["realization_score"]:
            deduped[key] = s
    scored = list(deduped.values())

    return scored, cumulative


# ==============================================================
# BLOCK 5: leading_signals 生成・sector_boosts 計算
# ==============================================================

def _build_leading_signals(
    scored_items: list[dict],
    min_score:    float = 0.20,
) -> list[dict]:
    """
    スコアリング済みアイテムから leading_signals リストを生成する。
    LEADING_INDICATOR_RULES 互換形式。

    $final\\_adj = base\\_score \times realization\\_score$
    min_score 未満の実現度スコアは除外。
    """
    signals = []
    seen_keywords: set = set()

    # 実現度スコア降順でソート
    sorted_items = sorted(
        scored_items,
        key=lambda x: x["realization_score"],
        reverse=True,
    )

    for item in sorted_items:
        kw    = item["keyword"]
        meta  = item["kw_meta"]
        score = item["realization_score"]

        if score < min_score:
            continue
        if kw in seen_keywords:
            continue   # 同キーワードは最高スコアのみ採用
        seen_keywords.add(kw)

        sectors   = meta.get("sectors", [])
        direction = meta.get("direction", "tailwind")

        signal_def = {
            "rule_id":   f"policy_radar_{kw.replace(' ', '_')}",
            "label":     f"政策レーダー: {meta.get('label', kw)}",
            "lag_weeks": item["lag_weeks"],
            "signal": {
                "tailwind": sectors if direction == "tailwind" else [],
                "headwind": sectors if direction == "headwind" else [],
            },
            "rationale": {
                sc: (f"{meta.get('label', kw)}の影響（実現度={score:.2f}, "
                     f"出所={item['source_name']}）")
                for sc in sectors
            },
            "triggered_val": score,
            "source":        item.get("source_name", ""),
            "title":         item.get("title", ""),
            "link":          item.get("link", ""),
        }
        signals.append(signal_def)

    return signals


def _build_sector_boosts(
    scored_items: list[dict],
    min_score:    float = 0.40,
    max_adj:      float = 7.0,
) -> dict[str, float]:
    """
    スコアリング済みアイテムからセクター別ブースト量を計算する。
    実現度スコアが min_score 以上のシグナルのみ適用。

    $boost = base\\_score \times realization\\_score$
    同セクターに複数シグナルがある場合は加算。
    上限: max_adj にクランプ。

    Returns:
        {sector_code: boost_pt}
    """
    boosts: dict[str, float] = {}

    for item in scored_items:
        if item["realization_score"] < min_score:
            continue
        meta      = item["kw_meta"]
        sectors   = meta.get("sectors", [])
        direction = meta.get("direction", "tailwind")
        base      = float(meta.get("base_score", 3.0))
        adj       = base * item["realization_score"]
        if direction == "headwind":
            adj = -adj

        for sc in sectors:
            boosts[sc] = round(
                max(-max_adj, min(max_adj, boosts.get(sc, 0.0) + adj)), 2
            )

    return boosts


# ==============================================================
# BLOCK 6: イベントカレンダー統合（P3）
# ==============================================================

# 固定イベントパターン（外務省RSSで動的補完）
_FIXED_EVENT_PATTERNS: list[dict] = [
    {
        "keyword":     "G7",
        "label":       "G7サミット",
        "sectors":     ["1016", "1015", "1025"],
        "direction":   "tailwind",
        "lead_days":   14,   # イベント前14日以内でスコア上乗せ
    },
    {
        "keyword":     "日米",
        "label":       "日米首脳会談",
        "sectors":     ["1016", "1015", "1017"],
        "direction":   "tailwind",
        "lead_days":   7,
    },
    {
        "keyword":     "閣議",
        "label":       "閣議決定",
        "sectors":     [],   # キーワードマップで個別対応
        "direction":   "tailwind",
        "lead_days":   3,
    },
    {
        "keyword":     "予算委員会",
        "label":       "予算委員会",
        "sectors":     ["1003", "1021", "1014"],
        "direction":   "tailwind",
        "lead_days":   7,
    },
]


def _detect_events_30d(items: list[dict]) -> list[dict]:
    """
    今後30日以内の政策イベントをアイテムから検出する。

    Returns:
        [{"label": str, "sectors": [...], "lead_days": int,
          "source": str, "link": str, "detected_at": str}, ...]
    """
    events   = []
    now      = datetime.now()
    detected: set = set()

    for item in items:
        text = item.get("text", "")
        for pat in _FIXED_EVENT_PATTERNS:
            kw = pat["keyword"]
            if kw not in text:
                continue
            if kw in detected:
                continue
            detected.add(kw)
            events.append({
                "label":       pat["label"],
                "keyword":     kw,
                "sectors":     pat["sectors"],
                "direction":   pat["direction"],
                "lead_days":   pat["lead_days"],
                "source":      item.get("source_name", ""),
                "link":        item.get("link", ""),
                "detected_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            })

    return events


# ==============================================================
# BLOCK 7: Wave 4 Idea3-P2 — L3 自動昇格判定
# ==============================================================

def _promote_to_medium_cache(
    cumulative:    dict,
    scored_items:  list[dict],
    cfg,
) -> int:
    """
    累積スコアが _CUMULATIVE_THRESHOLD_FOR_L3（3件）以上の政策シグナルを
    policy_medium_cache（L3）に自動昇格する。

    昇格条件:
      cumulative[key]["count"] >= 3 AND realization_score 合計 >= 1.5

    policy_signal_applier.register_signals() を経由して
    policy_medium_cache.json に書き込む。

    Returns:
        int: 昇格件数
    """
    promoted = 0
    threshold_count = _CUMULATIVE_THRESHOLD_FOR_L3
    threshold_score = 1.5   # 累積 realization_score の合計閾値

    try:
        import sys as _sys, importlib as _imp, pathlib as _pl
        _sys.path.insert(0, str(_pl.Path(__file__).parent.parent))
        psa = _imp.import_module("policy_signal_applier")

        for key, cum in cumulative.items():
            count       = cum.get("count", 0)
            total_score = cum.get("total_score", 0.0)
            if count < threshold_count or total_score < threshold_score:
                continue

            sectors   = cum.get("sectors", [])
            direction = cum.get("direction", "tailwind")
            label     = cum.get("label", key)
            lag       = cum.get("lag_weeks", 16)

            # L3 キャッシュへの昇格
            # policy_medium_cache.json を直接操作する
            medium_path = Path(getattr(cfg, "CACHE_DIR", "/tmp")) / "policy_medium_cache.json"
            try:
                if medium_path.exists():
                    medium = json.loads(medium_path.read_text(encoding="utf-8"))
                else:
                    medium = {"signals": [], "last_updated": ""}

                today = datetime.now().strftime("%Y-%m-%d")
                # 既存エントリのキー確認
                existing_keys = {
                    s.get("policy_key") for s in medium.get("signals", [])
                }
                if key not in existing_keys:
                    boost_pt = min(10.0, total_score * 2.0)   # 最大+10pt
                    for sc in sectors:
                        medium.setdefault("signals", []).append({
                            "policy_key":    key,
                            "sector_code":   sc,
                            "base_adj":      round(boost_pt, 2),
                            "direction":     direction,
                            "registered_at": today,
                            "source":        "policy_radar_L3_auto",
                            "label":         label,
                            "lag_weeks":     lag,
                            "cumulative_count": count,
                        })
                    medium["last_updated"] = today
                    medium_path.write_text(
                        json.dumps(medium, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    print(
                        f"    ⭐ [policy_radar] L3自動昇格: "
                        f"「{label}」({count}ソース, "
                        f"累積score={total_score:.2f}) "
                        f"→ policy_medium_cache"
                    )
                    promoted += 1

            except Exception as e:
                print(f"    ⚠️  [policy_radar] L3昇格失敗 ({key}): "
                      f"{str(e)[:60]}")

    except Exception as e:
        print(f"    ⚠️  [policy_radar] L3昇格モジュールロード失敗: "
              f"{str(e)[:60]}")

    return promoted


# ==============================================================
# BLOCK 8: メインエントリポイント
# ==============================================================

def run_policy_radar(cfg, results: Optional[dict] = None) -> dict:
    """
    政策レーダーのメイン実行関数。
    stock_analyzer_main.py から _load_local_module 経由で呼ばれる。

    Args:
        cfg:     config モジュール
        results: stock_analyzer_main.results 辞書（None 可）

    Returns:
        {
          "total_signals":   int,
          "leading_signals": list,
          "events_30d":      list,
          "sector_boosts":   dict,
          "items":           list,
          "cumulative_hits": dict,
          "promoted_to_l3":  int,
          "cached":          bool,
          "timestamp":       str,
        }
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    print("\n  🎯 [policy_radar] 政策レーダー 開始...")
    t0 = datetime.now()

    # ── キャッシュ確認（POLICY_RADAR_INTENT_CACHE_TTL_DAYS＝既定30日以内）──
    cached = _load_radar_cache(cfg)
    if cached:
        elapsed = (datetime.now() - t0).seconds
        print(f"  ✅ [policy_radar] キャッシュ利用 ({elapsed}秒)")
        cached["cached"] = True
        return cached

    # ── 設定取得 ──────────────────────────────────────────────
    max_feeds   = int(getattr(cfg, "POLICY_RADAR_MAX_FEEDS", 10))
    kw_keywords = list(getattr(cfg, "KOKKAI_SEARCH_KEYWORDS",
                               ["半導体", "防衛費", "子育て支援",
                                "インバウンド", "GX"]))
    rss_feeds   = getattr(cfg, "GOV_RSS_FEEDS", []) or _DEFAULT_RSS_FEEDS
    keyword_map = _get_keyword_map(cfg)
    enable_pdf  = getattr(cfg, "ENABLE_PDF_EXTRACTION", True)

    # P2 実行判定（週次: 月曜日のみ）
    is_weekly = datetime.now().weekday() == 0   # 月曜=0

    # ── 累積スコア・PDF キャッシュ読み込み ───────────────────
    cumulative = _load_cumulative(cfg)
    pdf_cache  = _load_pdf_url_cache(cfg) if (enable_pdf and is_weekly) else {}

    all_items: list[dict] = []

    # ── P1: RSS 取得 ──────────────────────────────────────────
    rss_items = _fetch_rss_items(rss_feeds, max_feeds)
    all_items.extend(rss_items)

    # ── P1: 国会議事録API ──────────────────────────────────────
    kokkai_items = _fetch_kokkai_items(kw_keywords)
    all_items.extend(kokkai_items)

    # ── P2: PDF テキスト抽出（週次・月曜のみ）────────────────
    if enable_pdf and is_weekly:
        pdf_count = 0
        for item in all_items:
            if not item.get("has_pdf"):
                continue
            pdf_urls = _find_pdf_links_in_item(item)
            for purl in pdf_urls[:2]:   # 1アイテムあたり最大2件
                if purl in pdf_cache and pdf_cache[purl].get("text"):
                    item["text"] += " " + pdf_cache[purl]["text"]
                else:
                    pdf_text = _extract_pdf_text(purl, pdf_cache)
                    if pdf_text:
                        item["text"] += " " + pdf_text
                        pdf_count += 1
                time.sleep(0.5)
        if pdf_count:
            print(f"    📄 [policy_radar] PDF抽出: {pdf_count}件")
        _save_pdf_url_cache(cfg, pdf_cache)

    # ── P3: スコアリング ──────────────────────────────────────
    scored_items, cumulative = _score_items(all_items, keyword_map, cumulative, cfg=cfg)
    _save_cumulative(cfg, cumulative)

    # ── leading_signals 生成 ──────────────────────────────────
    leading_signals = _build_leading_signals(scored_items, min_score=0.20)

    # ── sector_boosts 計算 ────────────────────────────────────
    sector_boosts = _build_sector_boosts(scored_items, min_score=0.40)

    # ── イベントカレンダー ────────────────────────────────────
    events_30d = _detect_events_30d(all_items)

    # ── Wave 4 Idea3-P2: L3 自動昇格 ─────────────────────────
    promoted = _promote_to_medium_cache(cumulative, scored_items, cfg)

    # ── policy_signal_applier へのシグナル登録 ────────────────
    if sector_boosts:
        try:
            import sys as _sys, importlib as _imp, pathlib as _pl
            _sys.path.insert(0, str(_pl.Path(__file__).parent.parent))
            psa    = _imp.import_module("policy_signal_applier")
            p_cache = psa.load_policy_short_cache(cfg)
            p_cache = psa.register_signals(
                cache         = p_cache,
                sector_boosts = sector_boosts,
                source        = "policy_radar",
                politician    = "policy_radar_auto",
                asset         = "",
                direction     = "buy",
                cfg           = cfg,
            )
            psa.save_policy_short_cache(p_cache, cfg)
            print(
                f"    📋 [policy_radar] policy_short_cache に "
                f"{len(sector_boosts)}業種のシグナルを登録"
            )
        except Exception as e:
            print(f"    ⚠️  [policy_radar] policy_signal_applier 連携エラー: "
                  f"{str(e)[:60]}")

    # ── 結果組み立て ──────────────────────────────────────────
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    payload = {
        "total_signals":   len(leading_signals),
        "leading_signals": leading_signals,
        "events_30d":      events_30d,
        "sector_boosts":   sector_boosts,
        "items":           [
            {k: v for k, v in s.items() if k != "kw_meta"}
            for s in scored_items[:50]   # UI表示用に上位50件
        ],
        "cumulative_hits": {
            k: {"count": v["count"], "total_score": round(v["total_score"], 3),
                "label": v["label"], "last_seen": v["last_seen"]}
            for k, v in cumulative.items()
            if v["count"] >= 1
        },
        "promoted_to_l3":  promoted,
        "cached":          False,
        "timestamp":       now_str,
        # ── Wave 5 - Idea3-P3: 文意判定サマリー ──────────────
        "intent_summary": {
            s["keyword"]: {
                "intent":       s.get("intent_classification", {}).get("intent", "considering"),
                "intent_label": s.get("intent_classification", {}).get("intent_label", "検討中"),
                "multiplier":   s.get("intent_classification", {}).get("multiplier", 0.50),
                "method":       s.get("intent_classification", {}).get("method", "rule"),
                "realization_score_raw": s.get("realization_score_raw", s.get("realization_score", 0.0)),
                "realization_score":     s.get("realization_score", 0.0),
            }
            for s in scored_items
        },
    }

    # ── キャッシュ保存 ────────────────────────────────────────
    _save_radar_cache(cfg, payload)

    elapsed = (datetime.now() - t0).seconds
    print(
        f"\n  ✅ [policy_radar] 完了 ({elapsed}秒): "
        f"{len(all_items)}件取得 → "
        f"{len(scored_items)}件ヒット / "
        f"{len(leading_signals)}シグナル / "
        f"L3昇格{promoted}件"
    )

    return payload


# ==============================================================
# BLOCK 9: 単体テスト
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

    print("=== policy_radar 単体テスト ===\n")

    # ── モックアイテムでスコアリング検証 ──────────────────────
    mock_items = [
        {
            "title":       "半導体産業強化に向けた閣議決定について",
            "summary":     "本日、閣議において半導体補助金の拡充を決定した。",
            "link":        "https://example.go.jp/press/001",
            "published":   datetime.now().strftime("%Y-%m-%d"),
            "source_name": "経産省プレスリリース",
            "priority":    "high",
            "has_pdf":     False,
            "text":        "半導体補助金 閣議決定 経済対策 来年度予算",
        },
        {
            "title":       "防衛費増額に関する首相発言",
            "summary":     "首相は防衛費について早急に法改正を行うと述べた。",
            "link":        "https://example.go.jp/press/002",
            "published":   datetime.now().strftime("%Y-%m-%d"),
            "source_name": "首相官邸",
            "priority":    "high",
            "has_pdf":     False,
            "text":        "防衛費 防衛装備 首相 早急に 法改正を行う",
        },
    ]

    km  = _get_keyword_map(cfg)
    cum = {}
    scored, cum = _score_items(mock_items, km, cum)

    print("  ── スコアリング結果 ──")
    for s in scored:
        print(
            f"  [{s['source_name']}] 「{s['keyword']}」 "
            f"実現度={s['realization_score']:.2f} "
            f"lag={s['lag_weeks']}w "
            f"hit={s['hit_count']}"
        )

    signals = _build_leading_signals(scored, min_score=0.20)
    boosts  = _build_sector_boosts(scored, min_score=0.40)
    events  = _detect_events_30d(mock_items)

    print(f"\n  leading_signals: {len(signals)}件")
    for sig in signals:
        print(f"    {sig['rule_id']}: "
              f"tailwind={sig['signal']['tailwind']} "
              f"headwind={sig['signal']['headwind']} "
              f"score={sig['triggered_val']:.2f}")

    print(f"\n  sector_boosts: {boosts}")
    print(f"  events_30d: {len(events)}件")
    print("\n  ✅ 単体テスト完了")
