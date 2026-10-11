# ==============================================================
# arxiv_monitor.py  v2.0
# ArXiv 論文監視モジュール — 堅牢版
# ==============================================================
# 【v1.0 からの変更点サマリー】
#
#   [FIX-1] 0件問題の根本解決
#     v1.0: feedparser.parse(url) を直接呼ぶ
#           → Colab の SSL/UA 制限で entries=[] が返り 0 件になる
#     v2.0: requests で生 XML を取得 → feedparser.parse(xml文字列) に渡す
#           → feedparser の urllib を使わせない
#           → User-Agent・SSL を明示的に制御
#
#   [FIX-2] フォールバック追加
#     v1.0: rss.arxiv.org のみ（不安定）
#     v2.0: export.arxiv.org(primary) → rss.arxiv.org(fallback)
#           → feedparser.parse(url) 直接（最終手段）の 3 段構え
#
#   [FIX-3] リトライ処理を追加
#     v1.0: 失敗したら 0 件で終了
#     v2.0: 1 URL あたり最大 3 回（5s/10s 指数バックオフ）
#
#   [FIX-4] 取得ログの詳細化
#     v1.0: 「フィードが空です」の 1 行のみ
#     v2.0: FetchLogger クラスで試行 URL・メソッド・失敗理由を全記録
#           → 0 件になった場合の原因追跡が可能
#
#   [REFACTOR-1] 設定を config.py に集約
#     v1.0: ARXIV_FEEDS / INVESTMENT_KEYWORDS / パスを本ファイルに直書き
#     v2.0: import config as cfg で全設定を参照
#
#   [REFACTOR-2] スコアリングをタプル形式に対応
#     v1.0: キーワードを含むだけで同一点数（タイトルのみ 2 点, その他 1 点）
#     v2.0: config.ARXIV_KEYWORDS の (keyword, title_pts, summary_pts) を参照
#
# 【依存関係】
#   import: config.py のみ（循環参照なし）
# ==============================================================

import subprocess, sys

def _install():
    pkgs = ["feedparser", "requests", "urllib3"]
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "-q"] + pkgs,
        capture_output=True,
    )
_install()

import feedparser  # type: ignore
import requests  # type: ignore
import json
import time
import re
from pathlib import Path
from datetime import datetime
from datetime import timedelta
from typing import Optional

# ── config.py をロード（/content/ または 同ディレクトリから解決） ──
def _load_config():
    try:
        import config
        return config
    except ImportError:
        import importlib.util
        for p in [
            "/content/config.py",
            str(Path(__file__).parent / "config.py"),
        ]:
            if Path(p).exists():
                spec = importlib.util.spec_from_file_location("config", p)
                # spec / loader が取れないパスはスキップ
                if spec is None or spec.loader is None:
                    continue
                mod  = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                sys.modules["config"] = mod
                return mod
        raise ImportError(
            "config.py が見つかりません。"
            "/content/ に配置するか、同ディレクトリに置いてください。"
        )

cfg = _load_config()


# ==============================================================
# BLOCK 1: 取得詳細ログ管理（FetchLogger）
# ==============================================================
# 【v1.0 との差分】新規クラス
#   v1.0 では失敗の原因が全く分からなかった。
#   このクラスで「どの URL で・何回目で・なぜ失敗したか」を記録する。

class FetchLogger:
    """
    ArXiv フィード取得の成否を詳細に記録するロガー。
    各試行の URL・使用メソッド・HTTP エラー理由を保持する。
    """

    def __init__(self):
        self._records: list = []

    def record(
        self,
        category: str,
        url: str,
        method: str,
        status: str,
        count: int,
        reason: str = "",
    ) -> dict:
        """
        1 試行の結果を記録してコンソールにも出力する。

        Args:
            category: "q-fin.PM" など
            url:      試行した URL
            method:   "requests_primary" / "requests_fallback" / "feedparser_direct"
            status:   "success" / "empty" / "error"
            count:    取得できた論文数
            reason:   失敗理由（エラーメッセージなど）
        """
        icon = "✅" if status == "success" else ("⚠️" if status == "empty" else "❌")
        entry = {
            "ts":       datetime.now().strftime("%H:%M:%S"),
            "category": category,
            "url":      url,
            "method":   method,
            "status":   status,
            "count":    count,
            "reason":   reason,
        }
        self._records.append(entry)
        reason_disp = f" | {reason[:70]}" if reason else ""
        print(f"    {icon} [{method}] {status} | {count}件{reason_disp}")
        return entry

    def summary(self) -> dict:
        """全カテゴリのサマリーを表示して辞書で返す"""
        total   = sum(r["count"] for r in self._records)
        success = [r for r in self._records if r["status"] == "success"]
        empty   = [r for r in self._records if r["status"] == "empty"]
        errors  = [r for r in self._records if r["status"] == "error"]

        print(f"\n  ── 取得サマリー ────────────────────────────────")
        print(f"  合計取得数  : {total} 件")
        print(f"  成功カテゴリ: {len(success)} / {len(cfg.ARXIV_FEEDS)}")
        if empty:
            print(f"  ⚠️  空フィード: {[r['category'] for r in empty]}")
        if errors:
            for r in errors:
                print(f"  ❌ {r['category']}: {r['reason'][:80]}")

        # 0 件のとき診断情報を表示（v1.0 にはなかった）
        if total == 0:
            print(f"\n  ── 0件診断 ─────────────────────────────────────")
            print(f"  考えられる原因と確認コマンド:")
            print(f"  1. ArXiv 一時過負荷 → 数分後に再実行")
            print(f"     !curl -sI https://export.arxiv.org/rss/q-fin.PM | head -5")
            print(f"  2. SSL エラー → urllib3 を更新")
            print(f"     !pip install urllib3 --upgrade -q")
            print(f"  3. feedparser バージョン不整合")
            print(f"     !pip install feedparser==6.0.11 -q")
            print(f"  詳細ログ: {cfg.LOG_DIR}/arxiv_fetch_log.json を確認")

        return {
            "total":         total,
            "success_count": len(success),
            "empty_count":   len(empty),
            "error_count":   len(errors),
        }

    def save(self, path: Path):
        """取得ログを JSON で保存（デバッグ・振り返り用）"""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(self._records, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            print(f"  ⚠️  FetchLogger 保存失敗: {e}")


# ==============================================================
# BLOCK 2: 堅牢な RSS フィード取得
# ==============================================================

def _fetch_xml_via_requests(url: str) -> str:
    """
    requests で生 XML を取得して文字列で返す。
    失敗時は例外を raise する（呼び出し元でハンドルする設計）。

    【v1.0 との差分】
      v1.0 にはこの関数が存在しなかった。
      feedparser.parse(url) を直接呼ぶと UA / SSL 問題で 0 件になるため
      この関数経由で XML を取得し feedparser.parse(xml_str) に渡す。
    """
    r = requests.get(
        url,
        headers=cfg.ARXIV_HEADERS,
        timeout=cfg.ARXIV_REQUEST_TIMEOUT,
    )
    r.raise_for_status()
    return r.text


def _extract_papers(category: str, entries: list) -> list:
    """feedparser エントリーリストから論文情報を抽出する"""
    papers = []
    for entry in entries[: cfg.ARXIV_MAX_PER_FEED]:
        # ArXiv ID の抽出
        raw_id   = getattr(entry, "id", "") or ""
        m        = re.search(r"abs/(.+?)(?:v\d+)?$", raw_id)
        arxiv_id = m.group(1).strip() if m else raw_id.strip()

        # HTML タグを除去してサマリーをクリーニング
        summary_raw = entry.get("summary", "") or ""
        summary     = re.sub(r"<[^>]+>", "", summary_raw).replace("\n", " ").strip()

        # 著者（3 名まで）
        authors = ""
        if hasattr(entry, "authors") and entry.authors:
            authors = ", ".join(a.get("name", "") for a in entry.authors[:3])
        elif hasattr(entry, "author"):
            authors = str(entry.author or "")

        papers.append({
            "id":        arxiv_id,
            "category":  category,
            "title":     entry.get("title", "").replace("\n", " ").strip(),
            "summary":   summary[:600],
            "authors":   authors[:150],
            "published": entry.get("published", ""),
            "url":       f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else "",
        })
    return papers


def _try_fetch_with_retry(
    url: str,
    method_label: str,
    category: str,
    fetch_log: FetchLogger,
    use_direct: bool = False,
) -> list:
    """
    1 つの URL に対してリトライ付きで取得を試みる。

    Args:
        use_direct: True のとき feedparser に URL を直接渡す（最終手段）
    """
    for attempt in range(1, cfg.ARXIV_RETRY_COUNT + 1):
        print(
            f"    試行 {attempt}/{cfg.ARXIV_RETRY_COUNT}: "
            f"[{method_label}] {url[:65]}..."
        )
        try:
            if use_direct:
                feed = feedparser.parse(url)
            else:
                xml_text = _fetch_xml_via_requests(url)
                feed     = feedparser.parse(xml_text)

            # bozo=True かつ entries 空は parse 失敗
            if feed.bozo and not feed.entries:
                reason = (
                    str(feed.bozo_exception)[:80]
                    if feed.bozo_exception
                    else "parse error (bozo)"
                )
                fetch_log.record(category, url, method_label, "error", 0,
                                 f"bozo: {reason}")
            elif not feed.entries:
                # HTTP は成功したが entries が空（週末・休日などで論文なし）
                fetch_log.record(category, url, method_label, "empty", 0,
                                 f"entries 空 (bozo={feed.bozo})")
                return []  # 空フィードはリトライしても変わらない
            else:
                papers = _extract_papers(category, feed.entries)
                fetch_log.record(category, url, method_label, "success",
                                 len(papers))
                return papers

        except requests.exceptions.SSLError as e:
            fetch_log.record(category, url, method_label, "error", 0,
                             f"SSLError: {str(e)[:60]}")
        except requests.exceptions.Timeout:
            fetch_log.record(category, url, method_label, "error", 0,
                             f"Timeout ({cfg.ARXIV_REQUEST_TIMEOUT}s)")
        except requests.exceptions.HTTPError as e:
            code = e.response.status_code if e.response else "?"
            fetch_log.record(category, url, method_label, "error", 0,
                             f"HTTP {code}")
            if str(code).startswith("4"):
                break  # 4xx はリトライしても無意味
        except requests.exceptions.ConnectionError as e:
            fetch_log.record(category, url, method_label, "error", 0,
                             f"ConnectionError: {str(e)[:60]}")
        except Exception as e:
            fetch_log.record(category, url, method_label, "error", 0,
                             str(e)[:80])

        if attempt < cfg.ARXIV_RETRY_COUNT:
            wait = cfg.ARXIV_RETRY_WAIT_SEC * attempt  # 5s, 10s
            print(f"    ⏳ {wait}秒待機後リトライ...")
            time.sleep(wait)

    return []


def fetch_single_category(
    category: str, urls: dict, fetch_log: FetchLogger
) -> list:
    """
    1 カテゴリについて以下の順で取得を試みる:
      1. requests + primary URL → feedparser（推奨・安定）
      2. requests + fallback URL → feedparser
      3. feedparser.parse(primary_url) 直接（最終手段）
    各段でリトライする。

    config.ARXIV_FEEDS の primary は arXiv API(export.arxiv.org/api/query)。
      日次RSS(export.arxiv.org/rss)は低頻度カテゴリで entries=0 が正常応答になり得るため使わない。
    """
    primary  = urls["primary"]
    fallback = urls["fallback"]

    # 試行 1: requests + primary
    papers = _try_fetch_with_retry(primary, "requests_primary", category, fetch_log)
    if papers:
        return papers

    # 試行 2: requests + fallback
    print(f"  ⤵️  {category}: primary 失敗 → fallback URL を試行")
    papers = _try_fetch_with_retry(fallback, "requests_fallback", category, fetch_log)
    if papers:
        return papers

    # 試行 3: feedparser 直接（最終手段）
    print(f"  ⤵️  {category}: feedparser 直接呼び出しを試行（最終手段）")
    return _try_fetch_with_retry(
        primary, "feedparser_direct", category, fetch_log, use_direct=True
    )


def fetch_all_feeds() -> tuple:
    """
    全カテゴリのフィードを取得する。

    【v1.0 との差分】
      v1.0: (list,) のみ返す
      v2.0: (papers: list, fetch_log: FetchLogger) のタプルを返す

    Returns:
        (all_papers, fetch_log)
    """
    fetch_log  = FetchLogger()
    all_papers = []

    print(f"\n  {'─'*52}")
    print(f"  📡 ArXiv フィード取得開始（{len(cfg.ARXIV_FEEDS)} カテゴリ）")
    print(f"  {'─'*52}")

    for category, urls in cfg.ARXIV_FEEDS.items():
        print(f"\n  📌 {category} [{urls['label']}]")
        papers = fetch_single_category(category, urls, fetch_log)
        all_papers.extend(papers)
        time.sleep(1)  # カテゴリ間インターバル（サーバー負荷軽減）

    fetch_log.summary()
    return all_papers, fetch_log


# ==============================================================
# BLOCK 3: 既読管理
# ==============================================================
# 【v1.0 との差分】
#   ・SEEN_PAPERS_PATH の定数参照を cfg.DATA_DIR に変更

def load_seen_papers() -> set:
    """既読論文 ID をセットで返す"""
    path = cfg.DATA_DIR / "arxiv_seen.json"
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return set(data.get("seen_ids", []))
        except Exception:
            return set()
    return set()


def save_seen_papers(seen: set):
    """既読論文 ID を保存する（最新 5000 件を保持）"""
    path = cfg.DATA_DIR / "arxiv_seen.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "seen_ids": list(seen)[-5000:],
                "updated":  datetime.now().isoformat(),
                "count":    len(seen),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

# ==============================================================
# BLOCK 3.5: Semantic Scholar API + 引用数キャッシュ（Idea1）
# ==============================================================
# citation_cache.json 形式:
#   {"arxiv_id": {"count": int, "cached_at": "YYYY-MM-DD"}, ...}
# TTL: SEMANTIC_SCHOLAR_CACHE_TTL_DAYS（デフォルト30日）

import time as _time

_SS_REQUEST_INTERVAL = 3.0   # Semantic Scholar 公開API レート制限（3秒/req）
from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'B5_ARXIV_MONITOR_SS_TIMEOUT': '_SS_TIMEOUT',
    'B6_ARXIV_MONITOR_SS_REQUEST_INTERVAL': '_SS_REQUEST_INTERVAL',
}

_SS_TIMEOUT          = 15    # タイムアウト（秒）


def _citation_cache_path() -> "Path":
    d = Path(getattr(cfg, "CACHE_DIR", "stock_data/cache"))
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d / "citation_cache.json"


def _load_citation_cache() -> dict:
    """citation_cache.json を読み込む。失敗時は空 dict を返す。"""
    try:
        p = _citation_cache_path()
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_citation_cache(cache: dict) -> None:
    """citation_cache.json を保存する。失敗時はサイレントに無視する。"""
    try:
        _citation_cache_path().write_text(
            json.dumps(cache, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def _extract_arxiv_id(raw_id: str) -> str:
    """
    ArXiv ID を正規化する。
    例: "http://arxiv.org/abs/2401.12345v1" → "2401.12345"
    """
    try:
        if "abs/" in raw_id:
            raw_id = raw_id.split("abs/")[-1]
        return raw_id.split("v")[0].strip()
    except Exception:
        return raw_id


def _fetch_citation_count_api(arxiv_id: str) -> int:
    """
    Semantic Scholar Graph API から引用数を直接取得する。
    失敗時は 0 を返す（例外を外に投げない）。
    """
    try:
        import requests as _req
        url_tmpl = getattr(
            cfg,
            "SEMANTIC_SCHOLAR_API_URL",
            "https://api.semanticscholar.org/graph/v1/paper/"
            "arXiv:{arxiv_id}?fields=citationCount",
        )
        url  = url_tmpl.replace("{arxiv_id}", arxiv_id)
        resp = _req.get(url, timeout=_SS_TIMEOUT)
        if resp.status_code == 200:
            return int(resp.json().get("citationCount", 0))
        # 404: 未収録（正常）/ 429: レート制限 → 0
        return 0
    except Exception:
        return 0


def get_citation_count(arxiv_id: str) -> int:
    """
    キャッシュ付きで引用数を返す。
    ・キャッシュ有効（TTL内） → キャッシュ値を即返却（API呼び出しなし）
    ・キャッシュ失効 or 未収録 → Semantic Scholar API を呼び出してキャッシュ更新

    Args:
        arxiv_id: 生の ArXiv ID（URL形式でも可）

    Returns:
        int: citationCount（取得失敗時は 0）
    """
    try:
        clean_id = _extract_arxiv_id(arxiv_id)
        ttl_days = int(getattr(cfg, "SEMANTIC_SCHOLAR_CACHE_TTL_DAYS", 30))
        today    = datetime.now().strftime("%Y-%m-%d")
        cutoff   = (datetime.now() - timedelta(days=ttl_days)).strftime("%Y-%m-%d")

        cache = _load_citation_cache()

        # ── キャッシュヒット ──────────────────────────────────────
        if clean_id in cache:
            entry = cache[clean_id]
            if entry.get("cached_at", "0000") >= cutoff:
                return int(entry.get("count", 0))

        # ── API 取得（レート制限を守る）──────────────────────────
        _time.sleep(_SS_REQUEST_INTERVAL)
        count = _fetch_citation_count_api(clean_id)

        # ── キャッシュ更新 + TTL 切れエントリ除去 ────────────────
        cache[clean_id] = {"count": count, "cached_at": today}
        cache = {k: v for k, v in cache.items()
                 if v.get("cached_at", "0000") >= cutoff}
        if len(cache) > 10_000:
            # 古いエントリを削除して10000件以内に収める
            sorted_items = sorted(
                cache.items(), key=lambda x: x[1].get("cached_at", "")
            )
            cache = dict(sorted_items[-10_000:])
        _save_citation_cache(cache)

        return count

    except Exception:
        return 0

# ==============================================================
# BLOCK 4: スコアリング（Idea1: 5乗数重み付け式）
# ==============================================================
# 【基礎スコア】
#   cfg.ARXIV_KEYWORDS は (keyword, title_pts, summary_pts) のタプルリスト。
#   keyword ごとに点数を個別設定でき、一致した点数を単純加算する
#   （文字列だけの要素はタイトル→2点、その他→1点）。
# 【最終スコア】
#   以下の5乗数を基礎スコアに乗算して最終スコアを算出する
#
#   $final = base\_kw\_score \times W_{inst} \times W_{cite}
#            \times W_{cat} \times W_{repro} \times W_{hot}$
#
#   W_inst: 著者機関スコア    （1.0〜1.8）トップ機関ほど高い
#   W_cite: 引用数スコア      （1.0〜2.0）Semantic Scholar API（30日キャッシュ）
#   W_cat:  カテゴリ整合スコア（0.7〜1.3）投資関連カテゴリほど高い
#   W_repro: 再現性スコア     （1.0〜1.3）コード・データセット公開で加算
#   W_hot:  ホット度スコア    （0.9〜1.2）公開直後ほど高い
#
#   ⚠️ base_kw_score = 0 の論文（キーワード不一致）は最終スコアも 0

# 投資関連カテゴリ → カテゴリ整合倍率
_CAT_MULTIPLIER: dict = {
    "q-fin":     1.30,   # 定量金融（最高）
    "cs.AI":     1.25,
    "cs.LG":     1.20,   # 機械学習
    "stat.ML":   1.20,
    "cs.CL":     1.15,   # 自然言語処理
    "cs.CV":     1.10,   # コンピュータビジョン
    "econ":      1.20,
    "cs.IR":     1.10,   # 情報検索
    "cs.NE":     1.05,
}
_CAT_MULTIPLIER_DEFAULT = 0.90

# トップ機関キーワード → 著者機関倍率
_TOP_INSTITUTION_KEYWORDS: dict = {
    # テック系研究機関（最高評価）
    "google":           1.80,
    "deepmind":         1.80,
    "openai":           1.80,
    "anthropic":        1.80,
    "meta ai":          1.75,
    "microsoft research": 1.75,
    "nvidia":           1.70,
    # 北米トップ大学
    "mit":              1.65,
    "stanford":         1.65,
    "berkeley":         1.60,
    "carnegie mellon":  1.60,
    "cmu":              1.60,
    "princeton":        1.55,
    "harvard":          1.55,
    "columbia":         1.50,
    # 欧州・アジアトップ機関
    "oxford":           1.55,
    "cambridge":        1.55,
    "eth":              1.50,
    "tsinghua":         1.45,
    "peking":           1.45,
    "tokyo":            1.35,
}
_INST_MULTIPLIER_DEFAULT = 1.00


def score_paper(paper: dict) -> tuple:
    """
    5乗数重み付け式でスコアリングする（Idea1）。

    $final = base\\_kw\\_score \times W_{inst} \times W_{cite}
             \times W_{cat} \times W_{repro} \times W_{hot}$

    Returns:
        (score: int, matched_keywords: list[str])
        matched_keywords にはキーワードヒット情報と乗数根拠を含む
    """
    try:
        title   = paper.get("title",   "").lower()
        summary = paper.get("summary", "").lower()
        authors = paper.get("authors", "").lower()
        category= paper.get("category","")
        pub_str = paper.get("published","")
        arxiv_id= paper.get("id", "")

        # ── ① 基礎スコア（キーワード一致）────────────────────────
        base_score = 0
        matched: list[str] = []

        for item in cfg.ARXIV_KEYWORDS:
            if isinstance(item, (list, tuple)) and len(item) == 3:
                kw, title_pts, summary_pts = item
            else:
                kw, title_pts, summary_pts = str(item), 2, 1

            kw_l = kw.lower()
            if kw_l in title:
                base_score += title_pts
                matched.append(f"[T+{title_pts}]{kw}")
            elif kw_l in summary:
                base_score += summary_pts
                matched.append(f"[S+{summary_pts}]{kw}")

        # キーワード不一致 → 乗数計算不要
        if base_score == 0:
            return 0, matched

        # ── ② W_inst: 著者機関スコア ─────────────────────────────
        w_inst = _INST_MULTIPLIER_DEFAULT
        for kw, mult in _TOP_INSTITUTION_KEYWORDS.items():
            if kw in authors:
                w_inst = max(w_inst, mult)
        if w_inst > _INST_MULTIPLIER_DEFAULT:
            matched.append(f"[×機関{w_inst:.2f}]")

        # ── ③ W_cite: 引用数スコア（Semantic Scholar）──────────────
        # 引用数取得はコストが高いため、base_score が高い論文のみ実行
        w_cite = 1.0
        if base_score >= 2 and arxiv_id:
            try:
                cite_count = get_citation_count(arxiv_id)
                if cite_count >= 50:
                    w_cite = 2.0
                    matched.append(f"[×引用{w_cite:.1f}({cite_count}件)]")
                elif cite_count >= 20:
                    w_cite = 1.7
                    matched.append(f"[×引用{w_cite:.1f}({cite_count}件)]")
                elif cite_count >= 6:
                    w_cite = 1.4
                    matched.append(f"[×引用{w_cite:.1f}({cite_count}件)]")
                elif cite_count >= 1:
                    w_cite = 1.2
                    matched.append(f"[×引用{w_cite:.1f}({cite_count}件)]")
                # cite_count = 0: 新着または未収録 → 1.0のまま
            except Exception:
                pass   # 引用数取得失敗 → 1.0 のまま

        # ── ④ W_cat: カテゴリ整合スコア ─────────────────────────
        w_cat = _CAT_MULTIPLIER_DEFAULT
        for cat_prefix, mult in _CAT_MULTIPLIER.items():
            if category.startswith(cat_prefix):
                w_cat = max(w_cat, mult)
        if w_cat > _CAT_MULTIPLIER_DEFAULT:
            matched.append(f"[×カテゴリ{w_cat:.2f}({category})]")

        # ── ⑤ W_repro: 再現性スコア ─────────────────────────────
        repro_kws = ["github", "code available", "open source",
                     "dataset", "open-source", "implementation"]
        w_repro = 1.0
        for kw in repro_kws:
            if kw in summary:
                w_repro = 1.30
                matched.append("[×再現性1.30(コード/データ公開)]")
                break
        if w_repro == 1.0:
            bench_kws = ["benchmark", "experiment", "evaluation"]
            if any(k in summary for k in bench_kws):
                w_repro = 1.15
                matched.append("[×再現性1.15(実験あり)]")

        # ── ⑥ W_hot: ホット度スコア（公開直後ほど高い）──────────
        w_hot = 1.0
        try:
            if pub_str:
                pub_dt = datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
                days_old = (datetime.now(pub_dt.tzinfo) - pub_dt).days
                if days_old <= 1:
                    w_hot = 1.20
                    matched.append("[×ホット1.20(今日/昨日)]")
                elif days_old <= 3:
                    w_hot = 1.10
                    matched.append("[×ホット1.10(3日以内)]")
                elif days_old <= 7:
                    w_hot = 1.05
                elif days_old > 30:
                    w_hot = 0.90
        except Exception:
            pass

        # ── ⑦ 最終スコア計算 ─────────────────────────────────────
        # $final = base \times W_{inst} \times W_{cite}
        #          \times W_{cat} \times W_{repro} \times W_{hot}$
        final = int(
            base_score * w_inst * w_cite * w_cat * w_repro * w_hot
        )
        return final, matched

    except Exception:
        # 全体エラー時はキーワード単純加算方式にフォールバック
        try:
            title   = paper.get("title",   "").lower()
            summary = paper.get("summary", "").lower()
            score, matched_fallback = 0, []
            for item in cfg.ARXIV_KEYWORDS:
                if isinstance(item, (list, tuple)) and len(item) == 3:
                    kw, t_pt, s_pt = item
                else:
                    kw, t_pt, s_pt = str(item), 2, 1
                kw_l = kw.lower()
                if kw_l in title:
                    score += t_pt
                    matched_fallback.append(f"[T+{t_pt}]{kw}")
                elif kw_l in summary:
                    score += s_pt
                    matched_fallback.append(f"[S+{s_pt}]{kw}")
            return score, matched_fallback
        except Exception:
            return 0, []


def filter_new_and_score(papers: list, seen: set) -> list:
    """未読論文のみ抽出してスコア降順で返す"""
    new_papers = [p for p in papers if p["id"] not in seen]
    skipped    = len(papers) - len(new_papers)
    print(f"\n  新着: {len(new_papers)} 件 | 既読スキップ: {skipped} 件")

    for p in new_papers:
        score, keywords = score_paper(p)
        p["investment_score"]  = score
        p["matched_keywords"]  = keywords

    return sorted(new_papers, key=lambda x: x["investment_score"], reverse=True)


# ==============================================================
# BLOCK 5: Markdown 生成・保存
# ==============================================================
# 【v1.0 との差分】
#   ・FetchLogger の記録を「取得ログ表」としてレポートに追記
#   ・論文概要の最大文字数を 300 → 400 に拡張

def papers_to_markdown(
    papers: list, threshold: int, fetch_log: FetchLogger
) -> str:
    high  = [p for p in papers if p["investment_score"] >= threshold]
    other = [p for p in papers if p["investment_score"] < threshold]

    md  = "# ArXiv 論文監視レポート\n\n"
    md += f"**取得日時**: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
    md += "## サマリー\n"
    md += f"- 新着論文: **{len(papers)}** 件\n"
    md += f"- 投資アイデア候補（スコア≥{threshold}）: **{len(high)}** 件\n"
    md += f"- その他: {len(other)} 件\n\n---\n\n"

    md += "## 🌟 投資アイデア候補論文\n\n"
    if not high:
        md += "*本日の高スコア論文はありませんでした*\n\n"
    else:
        for p in high:
            md += f"### [{p['title']}]({p['url']})\n"
            md += f"- **カテゴリ**: {p['category']}\n"
            md += f"- **著者**: {p['authors'][:100]}\n"
            md += f"- **スコア**: {p['investment_score']} 点\n"
            md += f"- **マッチ**: {', '.join(p['matched_keywords'][:6])}\n"
            md += f"- **概要**: {p['summary'][:400]}...\n\n"

    md += "---\n\n## 📄 その他の新着論文\n\n"
    md += "| カテゴリ | タイトル | スコア |\n|---------|---------|-------|\n"
    for p in other[:25]:
        t = p["title"][:55] + "..." if len(p["title"]) > 55 else p["title"]
        md += f"| {p['category']} | [{t}]({p['url']}) | {p['investment_score']} |\n"

    # 【v1.0 との差分】取得ログ表を追記（デバッグ用）
    md += "\n---\n\n## 📋 取得ログ（デバッグ用）\n\n"
    md += "| カテゴリ | メソッド | 結果 | 件数 | 備考 |\n"
    md += "|---------|---------|------|------|------|\n"
    for r in fetch_log._records:
        icon = "✅" if r["status"] == "success" else (
            "⚠️" if r["status"] == "empty" else "❌"
        )
        md += (
            f"| {r['category']} | {r['method']} "
            f"| {icon}{r['status']} | {r['count']} "
            f"| {r.get('reason','')[:40]} |\n"
        )

    md += "\n---\n*arxiv_monitor.py v2.0 — 投資判断は自己責任で*\n"
    return md


def save_arxiv_report(
    papers: list, threshold: int, fetch_log: FetchLogger
) -> Path:
    cfg.ARXIV_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    path  = cfg.ARXIV_DIR / f"arxiv_report_{today}.md"
    path.write_text(
        papers_to_markdown(papers, threshold, fetch_log),
        encoding="utf-8",
    )
    print(f"  ✅ レポート保存: {path}")
    return path


def update_idea_index():
    """全期間の高スコア論文インデックスを更新する"""
    index_path = cfg.ARXIV_DIR / "idea_index.md"
    reports    = sorted(cfg.ARXIV_DIR.glob("arxiv_report_*.md"), reverse=True)
    entries    = []
    for rp in reports[:30]:
        date_str = rp.stem.replace("arxiv_report_", "")
        try:
            content = rp.read_text(encoding="utf-8")
            for title, url in re.findall(r"### \[(.+?)\]\((.+?)\)", content):
                entries.append({"date": date_str, "title": title, "url": url})
        except Exception:
            pass

    today = datetime.now().strftime("%Y-%m-%d")
    md  = "# 投資アイデア候補 論文インデックス\n\n"
    md += f"更新日: {today} | 総計: {len(entries)} 件\n\n"
    md += "| 日付 | タイトル |\n|------|--------|\n"
    for e in entries:
        t = e["title"][:65] + "..." if len(e["title"]) > 65 else e["title"]
        md += f"| {e['date']} | [{t}]({e['url']}) |\n"
    index_path.write_text(md, encoding="utf-8")
    print(f"  ✅ アイデアインデックス更新: {len(entries)} 件")


# ==============================================================
# BLOCK 6: メイン実行エントリーポイント
# ==============================================================

def run_monitor(investment_threshold: int | None = None) -> dict:
    """
    ArXiv 監視を 1 回実行する。
    stock_analyzer_main.py から呼ばれる（直接実行も可能）。

    【v1.0 との差分】
      ・返り値に "fetch_log" キーを追加（取得ログの辞書）
      ・0 件時に診断情報を表示する

    Returns:
        {
          "new_papers":             int,
          "investment_ideas_found": int,
          "report_path":            str,
          "fetch_log":              list,  # ← 新規
        }
    """
    import config as cfg
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    threshold = investment_threshold or cfg.ARXIV_INVESTMENT_THRESHOLD

    print("=" * 60)
    print("  📚 ArXiv 論文監視 v2.0 開始")
    print("=" * 60)

    cfg.ARXIV_DIR.mkdir(parents=True, exist_ok=True)
    cfg.DATA_DIR.mkdir(parents=True, exist_ok=True)

    seen = load_seen_papers()
    print(f"  既読論文: {len(seen)} 件")

    # フィード取得（詳細ログ付き）
    all_papers, fetch_log = fetch_all_feeds()

    # 取得ログを JSON で保存（デバッグ用）
    fetch_log.save(cfg.LOG_DIR / "arxiv_fetch_log.json")

    if not all_papers:
        print("\n  ⚠️  全カテゴリで論文を取得できませんでした")
        print("  上記の「0件診断」を参照してください。")
        return {
            "new_papers":             0,
            "investment_ideas_found": 0,
            "report_path":            "",
            "fetch_log":              fetch_log._records,
        }

    new_papers = filter_new_and_score(all_papers, seen)
    high_score = [p for p in new_papers if p["investment_score"] >= threshold]
    print(f"  投資アイデア候補: {len(high_score)} 件（閾値 {threshold}点以上）")

    report_path = ""
    if new_papers:
        rp = save_arxiv_report(new_papers, threshold, fetch_log)
        report_path = str(rp)
        update_idea_index()

    # 正本は BigQuery analytics.arxiv_papers（md は閲覧用）。保存できなかった回は既読にしない（次回もう一度保存を試みる）
    bq_result = {}
    if new_papers:
        import arxiv_store
        bq_result = arxiv_store.store(new_papers, threshold, cfg)

    if not bq_result.get("error"):
        seen.update(p["id"] for p in new_papers)
        save_seen_papers(seen)

    print(f"\n  ✅ ArXiv 監視完了: 新着 {len(new_papers)} 件 | 候補 {len(high_score)} 件")
    if high_score:
        print(f"  ★ トップ: {high_score[0]['title'][:65]}...")

    return {
        "new_papers":             len(new_papers),
        "investment_ideas_found": len(high_score),
        "report_path":            report_path,
        "fetch_log":              fetch_log._records,
        "bq":                     bq_result,         # {"rows": n} / {"error": 理由}
        "papers":                 new_papers,        # thematic_etf_tracker連携用
        "all_papers":             list(all_papers),  # 全論文（キャッシュ済み含む）
    }

# 単体テスト用
if __name__ == "__main__":
    run_monitor()
