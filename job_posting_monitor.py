# ==============================================================
# job_posting_monitor.py  v1.0  (Wave 6 - I-1)
# ==============================================================
# 【役割】
#   Wantedly / Green 等の求人票情報を収集・解析し、
#   技術キーワード変化（人材投資シグナル）を検出して
#   JP業種別の sector_boost を生成する。
#
# 【シグナル生成ロジック】
#   1. 求人サイトのページを requests で取得し、
#      JOB_TECH_KEYWORDS（config.py BLOCK 26）にマッチする
#      キーワードを集計する
#   2. 前月キャッシュと比較し、急増（比率 >= JOB_SURGE_THRESHOLD_RATIO）
#      を検知する
#   3. 急増カテゴリを JOB_SURGE_SECTOR_MAP で JP業種コードに変換し、
#      sector_boost に JOB_SURGE_SCORE_BONUS を加算する
#
# 【キャッシュ】
#   CACHE_DIR/hiring/hiring_signal_cache.json
#     TTL: JOB_POSTING_CACHE_TTL_DAYS（30日）
#     スキーマ:
#       {
#         "last_updated": str,
#         "keyword_counts": {category: count},   # 今月分
#         "prev_counts":    {category: count},   # 前月分
#         "sector_boosts":  {sector_code: pt},
#         "surge_detected": [category, ...],
#         "raw_postings":   [{title, url, snippet, category}, ...],
#       }
#
# 【実行頻度】
#   stock_analyzer_main.py から週次バッチ（月曜）で呼ばれる。
#   TTL 内であればキャッシュを返し、外部への HTTP アクセスをスキップする。
#
# 【依存】
#   config.py (BLOCK 26: JOB_TECH_KEYWORDS / JOB_SURGE_THRESHOLD_RATIO /
#              JOB_SURGE_SCORE_BONUS / JOB_SURGE_SECTOR_MAP /
#              JOB_POSTING_CACHE_TTL_DAYS / JOB_POSTING_CACHE_PATH)
#   requests（HTTP取得）/ beautifulsoup4（HTML解析）
#
# 【呼び出し方（stock_analyzer_main.py から）】
#   _jpm = _load_local_module("job_posting_monitor", ...)
#   jpm_result = _jpm.run_job_posting_monitor(cfg, results)
#   results["job_posting"] = jpm_result
# ==============================================================

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


# ==============================================================
# BLOCK 1: キャッシュ管理
# ==============================================================

def _hiring_cache_dir(cfg) -> Path:
    """hiring キャッシュディレクトリを返す（なければ作成）。"""
    base = Path(getattr(cfg, "CACHE_DIR", "stock_data/cache"))
    d = base / "hiring"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_cache(cfg) -> dict:
    """
    hiring_signal_cache.json を読み込む。
    TTL 切れの場合は空 dict を返す。

    Returns:
        {
          "last_updated":  str,
          "keyword_counts": {category: int},
          "prev_counts":    {category: int},
          "sector_boosts":  {sector_code: float},
          "surge_detected": [category, ...],
          "raw_postings":   [{...}, ...],
        }
    """
    fname = getattr(cfg, "JOB_POSTING_CACHE_PATH", "hiring_signal_cache.json")
    p     = _hiring_cache_dir(cfg) / fname
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        ttl  = int(getattr(cfg, "JOB_POSTING_CACHE_TTL_DAYS", 30))
        last = data.get("last_updated", "")
        if last:
            age = (datetime.now() - datetime.fromisoformat(last)).days
            if age > ttl:
                return {}
        return data
    except Exception as e:
        print(f"    ⚠️  [job_posting] キャッシュ読み込み失敗: {str(e)[:60]}")
        return {}


def _save_cache(cfg, data: dict) -> None:
    """hiring_signal_cache.json を保存する。"""
    fname = getattr(cfg, "JOB_POSTING_CACHE_PATH", "hiring_signal_cache.json")
    p     = _hiring_cache_dir(cfg) / fname
    try:
        data["last_updated"] = datetime.now().isoformat()
        p.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"    ⚠️  [job_posting] キャッシュ保存失敗: {str(e)[:60]}")


# ==============================================================
# BLOCK 2: HTTP 取得ユーティリティ
# ==============================================================

_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

_REQUEST_TIMEOUT = 15   # 秒
_REQUEST_WAIT    = 2.0  # リクエスト間の待機秒数（サーバ負荷軽減）


def _safe_get(url: str, params: dict = None, timeout: int = _REQUEST_TIMEOUT) -> Optional[str]:
    """
    requests.get を try/except で保護し、HTML テキストを返す。
    失敗時は None を返す。

    Args:
        url:     リクエスト先 URL
        params:  クエリパラメータ（省略可）
        timeout: タイムアウト秒数

    Returns:
        str: HTML テキスト | None
    """
    try:
        import requests  # type: ignore
        resp = requests.get(
            url,
            params=params,
            headers=_DEFAULT_HEADERS,
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        print(f"    ⚠️  [job_posting] HTTP取得失敗 ({url[:60]}): {str(e)[:50]}")
        return None


# ==============================================================
# BLOCK 3: Wantedly スクレイパー
# ==============================================================

def _scrape_wantedly(keywords: list[str], max_pages: int = 3) -> list[dict]:
    """
    Wantedly のプロジェクト検索ページからキーワードに一致する
    求人票タイトル・スニペットを取得する。

    Wantedly の公開検索URL:
      https://www.wantedly.com/projects?query={keyword}&page={n}

    Args:
        keywords:  検索キーワードリスト
        max_pages: 取得する最大ページ数（1キーワードあたり）

    Returns:
        [{"title": str, "snippet": str, "url": str, "source": "wantedly"}, ...]
    """
    postings: list[dict] = []

    for kw in keywords[:5]:   # 1カテゴリあたり最大5キーワードに限定
        for page in range(1, max_pages + 1):
            url  = "https://www.wantedly.com/projects"
            html = _safe_get(url, params={"query": kw, "page": page})
            if not html:
                break

            try:
                from bs4 import BeautifulSoup  # type: ignore
                soup = BeautifulSoup(html, "html.parser")

                # Wantedly のプロジェクトカードセレクタ（2024年時点の構造）
                # タイトルタグ: <h3> または data-automation="project-title"
                cards = soup.find_all("div", class_=re.compile(r"ProjectCard|project-card", re.I))
                if not cards:
                    # フォールバック: h3 タグから取得
                    cards = soup.find_all("h3")

                for card in cards:
                    title   = card.get_text(strip=True)
                    snippet = ""
                    # 親要素からスニペットを取得
                    parent = card.find_parent()
                    if parent:
                        snippet = parent.get_text(separator=" ", strip=True)[:200]

                    if title:
                        postings.append({
                            "title":   title,
                            "snippet": snippet,
                            "url":     f"https://www.wantedly.com/projects?query={kw}",
                            "source":  "wantedly",
                        })

            except ImportError:
                # BeautifulSoup 未インストール時は正規表現でフォールバック
                titles = re.findall(
                    r'<h[23][^>]*>([^<]{5,100})</h[23]>',
                    html, re.IGNORECASE
                )
                for t in titles:
                    postings.append({
                        "title":   t.strip(),
                        "snippet": "",
                        "url":     f"https://www.wantedly.com/projects?query={kw}",
                        "source":  "wantedly",
                    })
            except Exception as e:
                print(f"    ⚠️  [job_posting] Wantedly パース失敗 ({kw}): "
                      f"{str(e)[:50]}")

            time.sleep(_REQUEST_WAIT)

            # 1ページで十分なカード数が取れた場合は次ページをスキップ
            if len(postings) >= 50:
                break

    return postings


# ==============================================================
# BLOCK 4: Green スクレイパー
# ==============================================================

def _scrape_green(keywords: list[str], max_pages: int = 2) -> list[dict]:
    """
    Green（転職サイト）のキーワード検索から求人票情報を取得する。

    Green の公開検索URL:
      https://www.green-japan.com/search_key?keyword={keyword}&page={n}

    Args:
        keywords:  検索キーワードリスト
        max_pages: 取得する最大ページ数

    Returns:
        [{"title": str, "snippet": str, "url": str, "source": "green"}, ...]
    """
    postings: list[dict] = []

    for kw in keywords[:3]:   # Green は負荷が高めのため3キーワードに限定
        for page in range(1, max_pages + 1):
            url  = "https://www.green-japan.com/search_key"
            html = _safe_get(url, params={"keyword": kw, "page": page})
            if not html:
                break

            try:
                from bs4 import BeautifulSoup  # type: ignore
                soup = BeautifulSoup(html, "html.parser")

                # Green の求人カードセレクタ（2024年時点）
                # タイトル: class="job-title" または h2/h3 タグ
                cards = soup.find_all(
                    attrs={"class": re.compile(r"job.?title|card.?title", re.I)}
                )
                if not cards:
                    cards = soup.find_all(["h2", "h3"])

                for card in cards:
                    title   = card.get_text(strip=True)
                    snippet = ""
                    parent  = card.find_parent()
                    if parent:
                        snippet = parent.get_text(separator=" ", strip=True)[:200]

                    if title and len(title) > 3:
                        postings.append({
                            "title":   title,
                            "snippet": snippet,
                            "url":     f"https://www.green-japan.com/search_key?keyword={kw}",
                            "source":  "green",
                        })

            except ImportError:
                titles = re.findall(
                    r'<h[23][^>]*>([^<]{5,100})</h[23]>',
                    html, re.IGNORECASE
                )
                for t in titles:
                    postings.append({
                        "title":   t.strip(),
                        "snippet": "",
                        "url":     f"https://www.green-japan.com/search_key?keyword={kw}",
                        "source":  "green",
                    })
            except Exception as e:
                print(f"    ⚠️  [job_posting] Green パース失敗 ({kw}): "
                      f"{str(e)[:50]}")

            time.sleep(_REQUEST_WAIT)

    return postings


# ==============================================================
# BLOCK 5: キーワードカウント集計
# ==============================================================

def _count_keywords(
    postings:  list[dict],
    keywords:  dict,
) -> dict[str, int]:
    """
    求人票リストに対してカテゴリ別キーワードカウントを集計する。

    Args:
        postings:  [{"title": str, "snippet": str, ...}, ...]
        keywords:  {category: [kw1, kw2, ...]}（config.JOB_TECH_KEYWORDS）

    Returns:
        {category: hit_count}
        hit_count: 全求人票における当該カテゴリのキーワードマッチ総数
    """
    counts: dict[str, int] = {cat: 0 for cat in keywords}

    for posting in postings:
        text = (
            posting.get("title",   "") + " " +
            posting.get("snippet", "")
        ).lower()

        for cat, kw_list in keywords.items():
            for kw in kw_list:
                if kw.lower() in text:
                    counts[cat] += 1

    return counts


# ==============================================================
# BLOCK 6: 急増検知・sector_boost 計算
# ==============================================================

def _detect_surge(
    curr_counts: dict[str, int],
    prev_counts: dict[str, int],
    cfg,
) -> tuple[list[str], dict[str, float]]:
    """
    前月比でキーワードカウントが急増しているカテゴリを検知し、
    sector_boost を計算する。

    急増判定:
      curr_count / max(prev_count, 1) >= JOB_SURGE_THRESHOLD_RATIO

    sector_boost 計算:
      $boost = JOB\\_SURGE\\_SCORE\\_BONUS$（急増カテゴリ → 対応業種コードへ加算）

    Args:
        curr_counts: 今月のカテゴリ別カウント
        prev_counts: 前月のカテゴリ別カウント（なければ空 dict）
        cfg:         config モジュール

    Returns:
        (surge_categories, sector_boosts)
        surge_categories: 急増と判定されたカテゴリ名リスト
        sector_boosts:    {sector_code: boost_pt}
    """
    threshold   = float(getattr(cfg, "JOB_SURGE_THRESHOLD_RATIO", 1.5))
    bonus_pt    = float(getattr(cfg, "JOB_SURGE_SCORE_BONUS",      5.0))
    sector_map  = getattr(cfg, "JOB_SURGE_SECTOR_MAP", {})

    surge_cats: list[str]       = []
    boosts:     dict[str, float] = {}

    for cat, curr in curr_counts.items():
        prev  = prev_counts.get(cat, 0)
        ratio = curr / max(prev, 1)

        if ratio >= threshold and curr >= 3:   # 最低3件以上ヒットが必要
            surge_cats.append(cat)
            print(
                f"    📈 [job_posting] 急増検知: {cat} "
                f"(prev={prev} → curr={curr}, ratio={ratio:.2f}x)"
            )
            for sc in sector_map.get(cat, []):
                boosts[sc] = round(boosts.get(sc, 0.0) + bonus_pt, 2)

    return surge_cats, boosts


# ==============================================================
# BLOCK 7: メインエントリポイント
# ==============================================================

def run_job_posting_monitor(cfg, results: dict) -> dict:
    """
    求人票モニターのメイン関数。
    stock_analyzer_main.py の週次バッチから呼ばれる。

    処理フロー:
      1. キャッシュ確認（TTL 30日以内ならキャッシュを返す）
      2. Wantedly / Green から求人票を取得
      3. JOB_TECH_KEYWORDS でカテゴリ別カウントを集計
      4. 前月比で急増カテゴリを検知
      5. sector_boost を生成してキャッシュに保存

    Args:
        cfg:     config モジュール
        results: stock_analyzer_main.results（現在は未使用。将来拡張用）

    Returns:
        {
          "sector_boosts":   {sector_code: float},
          "surge_detected":  [category, ...],
          "keyword_counts":  {category: int},
          "prev_counts":     {category: int},
          "total_postings":  int,
          "raw_postings":    [{...}, ...],   # 上位50件
          "cached":          bool,
          "timestamp":       str,
          "skipped":         bool,
        }
    """
    if not getattr(cfg, "ENABLE_JOB_POSTING_MONITOR", True):
        print("  ⏩ 求人票モニター: スキップ（ENABLE_JOB_POSTING_MONITOR=False）")
        return {"skipped": True, "sector_boosts": {}, "surge_detected": []}

    print("\n  📋 [job_posting] 求人票モニター 開始...")
    t0 = datetime.now()

    # ── キャッシュ確認 ────────────────────────────────────────
    cache = _load_cache(cfg)
    if cache:
        print(
            f"    📦 [job_posting] キャッシュ利用 "
            f"(急増={cache.get('surge_detected', [])}): "
            f"TTL内のためスキップ"
        )
        return {**cache, "cached": True}

    # ── キーワード設定取得 ────────────────────────────────────
    keywords    = getattr(cfg, "JOB_TECH_KEYWORDS", {})
    sources_cfg = getattr(cfg, "JOB_POSTING_SOURCES", {})

    if not keywords:
        print("    ⚠️  [job_posting] JOB_TECH_KEYWORDS が未設定 → スキップ")
        return {
            "skipped":       True,
            "sector_boosts": {},
            "surge_detected": [],
            "timestamp":     datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    # ── 求人票取得 ────────────────────────────────────────────
    all_postings: list[dict] = []

    # 全カテゴリのキーワードを結合して検索（カテゴリ単位でまとめて送る）
    all_keywords_flat: list[str] = []
    for kw_list in keywords.values():
        all_keywords_flat.extend(kw_list[:3])   # カテゴリあたり先頭3件

    # Wantedly
    wantedly_cfg = sources_cfg.get("wantedly", {})
    if wantedly_cfg.get("enabled", True):
        try:
            print("    🔍 [job_posting] Wantedly 取得中...")
            w_posts = _scrape_wantedly(all_keywords_flat, max_pages=2)
            all_postings.extend(w_posts)
            print(f"    ✅ [job_posting] Wantedly: {len(w_posts)}件取得")
        except Exception as e:
            print(f"    ⚠️  [job_posting] Wantedly 取得エラー（スキップ）: "
                  f"{str(e)[:60]}")

    # Green
    green_cfg = sources_cfg.get("green", {})
    if green_cfg.get("enabled", True):
        try:
            print("    🔍 [job_posting] Green 取得中...")
            g_posts = _scrape_green(all_keywords_flat, max_pages=2)
            all_postings.extend(g_posts)
            print(f"    ✅ [job_posting] Green: {len(g_posts)}件取得")
        except Exception as e:
            print(f"    ⚠️  [job_posting] Green 取得エラー（スキップ）: "
                  f"{str(e)[:60]}")

    print(f"    📊 [job_posting] 総取得: {len(all_postings)}件")

    # ── キーワードカウント集計 ────────────────────────────────
    curr_counts = _count_keywords(all_postings, keywords)
    prev_counts = cache.get("keyword_counts", {})   # 前回分（TTL切れ済み）

    print("    📊 [job_posting] カテゴリ別カウント:")
    for cat, cnt in sorted(curr_counts.items(), key=lambda x: -x[1]):
        prev = prev_counts.get(cat, 0)
        arrow = "↑" if cnt > prev else ("↓" if cnt < prev else "→")
        print(f"      {arrow} {cat}: {cnt}件 (前回: {prev}件)")

    # ── 急増検知・sector_boost 計算 ──────────────────────────
    surge_cats, sector_boosts = _detect_surge(curr_counts, prev_counts, cfg)

    if surge_cats:
        print(
            f"    🚨 [job_posting] 急増カテゴリ: {surge_cats} "
            f"→ sector_boost: {sector_boosts}"
        )
    else:
        print("    ✅ [job_posting] 急増なし")

    # ── キャッシュ保存 ────────────────────────────────────────
    # prev_counts には今回の curr_counts を引き継ぐ（次回の前月分として使用）
    new_cache = {
        "keyword_counts": curr_counts,
        "prev_counts":    prev_counts,   # 今回の前月分（TTL切れから引き継ぎ）
        "sector_boosts":  sector_boosts,
        "surge_detected": surge_cats,
        "raw_postings":   all_postings[:50],   # 上位50件のみ保存
    }
    _save_cache(cfg, new_cache)

    elapsed = (datetime.now() - t0).seconds
    print(
        f"\n  ✅ [job_posting] 完了 ({elapsed}秒): "
        f"取得{len(all_postings)}件 / 急増{len(surge_cats)}カテゴリ / "
        f"sector_boost={sector_boosts}"
    )

    return {
        "sector_boosts":  sector_boosts,
        "surge_detected": surge_cats,
        "keyword_counts": curr_counts,
        "prev_counts":    prev_counts,
        "total_postings": len(all_postings),
        "raw_postings":   all_postings[:50],
        "cached":         False,
        "timestamp":      datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "skipped":        False,
    }


# ==============================================================
# BLOCK 8: 単体テスト
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

    print("=== job_posting_monitor 単体テスト ===\n")

    # ── キーワードカウント検証 ─────────────────────────────────
    mock_postings = [
        {"title": "機械学習エンジニア募集",     "snippet": "LLMを使ったプロダクト開発 Deep Learning"},
        {"title": "AI Engineer",               "snippet": "生成AI基盤の設計・実装"},
        {"title": "半導体プロセス開発エンジニア", "snippet": "前工程 VLSI 設計"},
        {"title": "EV向けバッテリーマネジメント", "snippet": "全固体電池 BMS開発"},
        {"title": "Kubernetesエンジニア",       "snippet": "DX推進 クラウドネイティブ"},
    ]

    test_keywords = {
        "ai_ml":        ["機械学習", "LLM", "AI Engineer", "生成AI", "Deep Learning"],
        "semiconductor": ["半導体", "VLSI", "前工程"],
        "ev_battery":   ["EV", "全固体電池", "BMS"],
        "dx_cloud":     ["DX推進", "Kubernetes", "クラウドネイティブ"],
    }

    counts = _count_keywords(mock_postings, test_keywords)
    print("  ── カテゴリ別カウント ──")
    for cat, cnt in counts.items():
        print(f"  {cat}: {cnt}件")

    assert counts["ai_ml"] >= 3,        f"ai_ml={counts['ai_ml']} < 3"
    assert counts["semiconductor"] >= 2, f"semiconductor={counts['semiconductor']} < 2"
    assert counts["ev_battery"] >= 2,   f"ev_battery={counts['ev_battery']} < 2"
    assert counts["dx_cloud"] >= 2,     f"dx_cloud={counts['dx_cloud']} < 2"

    # ── 急増検知検証 ──────────────────────────────────────────
    prev = {"ai_ml": 1, "semiconductor": 0, "ev_battery": 0, "dx_cloud": 0}
    surge_cats, boosts = _detect_surge(counts, prev, cfg)
    print(f"\n  急増カテゴリ: {surge_cats}")
    print(f"  sector_boosts: {boosts}")

    assert "ai_ml" in surge_cats, "ai_ml が急増未検知"
    # ai_ml → 業種 "1016","1025","1033" にボーナス
    assert "1016" in boosts, "1016 が boosts に未登録"
    assert "1025" in boosts, "1025 が boosts に未登録"

    print("\n  ✅ 全テスト PASS")