# ==============================================================
# thematic_etf_tracker.py  v1.0  (Step C')
# テーマ型ETFトラッキング × ArXiv論文数 熱量スコアリング
# ==============================================================
# 【役割】
#   テーマ株ETF（量子/ウラン/リチウム/銅/宇宙/サイバー/AI/水素）の
#   市場モメンタムと ArXiv 論文数増加率を掛け合わせ、
#   「市場と学術の両方が同時に熱い」テーマを数値化して検出する。
#
# 【スコアリング設計】
#   ETFモメンタムスコア (0-100):
#     - 1ヶ月リターン（25%）
#     - 3ヶ月リターン（25%）
#     - 50日MA対比（25%）
#     - 出来高前月比（25%）
#
#   ArXivトレンドスコア (0-100):
#     - 直近4週間の論文数 vs 前4週間の論文数の増加率を正規化
#     - ArXiv取得が未実施の場合は 50（中立）で代替
#
#   熱量スコア (0-100):
#     = ETFモメンタムスコア × 0.60 + ArXivトレンドスコア × 0.40
#     根拠: 市場シグナルを学術より重視（投資判断の即時性）
#
# 【出力スキーマ】
#   {
#     "themes":         [ThemeResult, ...],   # 全テーマ結果
#     "top_themes":     [ThemeResult, ...],   # 熱量スコア上位N件
#     "timestamp":      str,
#   }
#   ThemeResult = {
#     theme_id, theme_name, etfs, etf_momentum_score,
#     arxiv_trend_score, heat_score,
#     etf_details: [{ticker, ret_1m, ret_3m, ma50_ratio, vol_ratio}],
#     arxiv_detail: {recent_4w, prev_4w, growth_rate},
#     signal: "hot"|"warming"|"neutral"|"cooling",
#   }
#
# 【依存】yfinance / config.py / results["arxiv"]（週次取得済みデータ）
# 【呼出】from thematic_etf_tracker import run_thematic_etf_tracker
# ==============================================================

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


# ==============================================================
# BLOCK 0: テーマ定義
# ==============================================================

THEME_DEFINITIONS = [
    {
        "id":       "quantum",
        "name":     "量子コンピュータ",
        "name_en":  "Quantum Computing",
        "etfs":     ["QTUM"],
        "arxiv_keywords": [
            "quantum computing", "quantum computer", "qubit",
            "quantum error correction", "quantum algorithm",
        ],
        "arxiv_api_cats": ["quant-ph"],
        "arxiv_categories": ["quant-ph", "cs.ET"],
    },
    {
        "id":       "uranium",
        "name":     "ウラン・原子力",
        "name_en":  "Uranium & Nuclear",
        "etfs":     ["URA", "NLR"],
        "arxiv_keywords": [
            "nuclear reactor", "small modular reactor", "nuclear power plant",
            "nuclear fuel", "uranium enrichment", "fusion reactor", "nuclear power",
        ],
        "arxiv_api_cats": ["nucl-ex", "nucl-th", "physics.plasm-ph", "physics.app-ph",
                           "physics.ins-det", "physics.soc-ph", "eess.SY"],
        "arxiv_categories": ["physics.app-ph"],
    },
    {
        "id":       "lithium",
        "name":     "リチウム・電池",
        "name_en":  "Lithium & Battery",
        "etfs":     ["LIT"],
        "arxiv_keywords": [
            "lithium-ion battery", "lithium battery", "solid-state battery",
            "solid-state electrolyte", "battery cathode", "sodium-ion battery",
        ],
        "arxiv_api_cats": ["cond-mat.mtrl-sci", "physics.chem-ph", "physics.app-ph",
                           "cond-mat.soft", "eess.SY"],
        "arxiv_categories": ["cond-mat.mtrl-sci"],
    },
    {
        "id":       "copper_metals",
        "name":     "銅・希少金属",
        "name_en":  "Copper & Rare Metals",
        "etfs":     ["COPX", "REMX"],
        "arxiv_keywords": [
            "critical minerals", "critical raw materials", "rare earth supply",
            "rare earth elements", "copper mining", "mineral exploration",
            "mineral deposits", "critical metals", "copper supply", "mining industry",
        ],
        "arxiv_api_cats": ["physics.geo-ph", "econ.GN", "q-fin.GN", "physics.soc-ph",
                           "cond-mat.mtrl-sci"],
        "arxiv_categories": ["cond-mat.mtrl-sci"],
    },
    {
        "id":       "space",
        "name":     "宇宙・航空",
        "name_en":  "Space & Aerospace",
        "etfs":     ["UFO", "ARKX"],
        "arxiv_keywords": [
            "satellite constellation", "launch vehicle", "rocket engine",
            "spacecraft", "low earth orbit", "space mission",
        ],
        "arxiv_api_cats": ["astro-ph.IM", "physics.space-ph", "eess.SP", "eess.SY",
                           "cs.NI", "cs.RO"],
        "arxiv_categories": ["astro-ph.IM"],
    },
    {
        "id":       "cybersecurity",
        "name":     "サイバーセキュリティ",
        "name_en":  "Cybersecurity",
        "etfs":     ["CIBR"],
        "arxiv_keywords": [
            "cybersecurity", "cyber attack", "intrusion detection",
            "ransomware", "malware", "vulnerability detection",
        ],
        "arxiv_api_cats": ["cs.CR"],
        "arxiv_categories": ["cs.CR"],
    },
    {
        "id":       "ai_infra",
        "name":     "AI・ロボティクス",
        "name_en":  "AI & Robotics",
        "etfs":     ["BOTZ"],
        "arxiv_keywords": [
            "large language model", "generative AI", "foundation model",
            "AI agent", "robot learning", "humanoid robot",
        ],
        "arxiv_api_cats": ["cs.AI", "cs.LG", "cs.CL", "cs.RO", "cs.CV"],
        "arxiv_categories": ["cs.AI", "cs.LG", "cs.RO"],
    },
    {
        "id":       "hydrogen",
        "name":     "水素・クリーンエネルギー",
        "name_en":  "Hydrogen & Clean Energy",
        "etfs":     ["ICLN"],
        "arxiv_keywords": [
            "green hydrogen", "hydrogen production", "water electrolysis",
            "fuel cell", "hydrogen storage", "electrolyzer",
        ],
        "arxiv_api_cats": ["cond-mat.mtrl-sci", "physics.chem-ph", "physics.app-ph",
                           "eess.SY", "physics.soc-ph"],
        "arxiv_categories": ["cond-mat.mtrl-sci", "physics.app-ph"],
    },
]

# 熱量シグナルの閾値

from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'TET_SIGNAL_THRESHOLDS': 'SIGNAL_THRESHOLDS',
}

SIGNAL_THRESHOLDS = {
    "hot":     70,   # 熱量スコア >= 70
    "warming": 50,   # 熱量スコア >= 50
    "neutral": 35,   # 熱量スコア >= 35
    # 35未満 = "cooling"
}


# ==============================================================
# BLOCK 1: ETFモメンタムデータ取得
# ==============================================================

def _fetch_etf_momentum(ticker: str) -> Optional[dict]:
    """
    1つのETFのモメンタム指標を yfinance で取得する。

    Returns:
        {ticker, ret_1m, ret_3m, ma50_ratio, vol_ratio, price}
        取得失敗時は None
    """
    try:
        import yf_cache as yf # type: ignore # yfinance は外部依存のため、実行環境にない場合はこのモジュール全体をスキップする設計

        t    = yf.Ticker(ticker)
        hist = t.history(period="6mo", interval="1d")

        if hist.empty or len(hist) < 22:
            return None

        close  = hist["Close"]
        volume = hist["Volume"]

        price_now  = float(close.iloc[-1])
        price_1m   = float(close.iloc[-22])   # 約1ヶ月前
        price_3m   = float(close.iloc[-66]) if len(close) >= 66 else float(close.iloc[0])

        # リターン計算
        ret_1m = (price_now - price_1m) / price_1m * 100 if price_1m > 0 else 0.0
        ret_3m = (price_now - price_3m) / price_3m * 100 if price_3m > 0 else 0.0

        # 50日移動平均との乖離率
        ma50       = float(close.tail(50).mean())
        ma50_ratio = (price_now - ma50) / ma50 * 100 if ma50 > 0 else 0.0

        # 出来高前月比（直近20日 vs 前20日）
        vol_recent = float(volume.iloc[-20:].mean()) if len(volume) >= 20 else 0.0
        vol_prev   = float(volume.iloc[-40:-20].mean()) if len(volume) >= 40 else vol_recent
        vol_ratio  = (vol_recent - vol_prev) / vol_prev * 100 if vol_prev > 0 else 0.0

        return {
            "ticker":     ticker,
            "price":      round(price_now, 2),
            "ret_1m":     round(ret_1m, 2),
            "ret_3m":     round(ret_3m, 2),
            "ma50_ratio": round(ma50_ratio, 2),
            "vol_ratio":  round(vol_ratio, 2),
        }
    except Exception as e:
        print(f"    ⚠️  ETF取得失敗 {ticker}: {str(e)[:50]}")
        return None


def _calc_etf_momentum_score(etf_details: list) -> float:
    """
    複数ETFのモメンタムデータを統合して0-100スコアを返す。

    スコア計算:
        1ヶ月リターン: [-20%, +20%] → [0, 100] に線形マッピング
        3ヶ月リターン: [-30%, +30%] → [0, 100]
        50日MA乖離:    [-15%, +15%] → [0, 100]
        出来高前月比:  [-50%, +50%] → [0, 100]
    各ETFのスコアを平均する（複数ETFある場合）。
    """
    if not etf_details:
        return 50.0   # データなしは中立

    def _clip_norm(val, lo, hi):
        """[lo, hi] の範囲を [0, 100] に線形正規化してクリップ"""
        return max(0.0, min(100.0, (val - lo) / (hi - lo) * 100))

    scores = []
    for d in etf_details:
        s1 = _clip_norm(d.get("ret_1m",   0), -20, 20)
        s3 = _clip_norm(d.get("ret_3m",   0), -30, 30)
        sm = _clip_norm(d.get("ma50_ratio", 0), -15, 15)
        sv = _clip_norm(d.get("vol_ratio", 0), -50, 50)
        score = s1 * 0.25 + s3 * 0.25 + sm * 0.25 + sv * 0.25
        scores.append(score)

    return round(sum(scores) / len(scores), 1)

# ==============================================================
# BLOCK 2: ArXivトレンドスコア算出 Semantic Scholar API 統合（⑤ 論文スコアリング強化）
# ==============================================================

_SS_CACHE: dict = {}   # セッション内キャッシュ（重複APIコール防止）

def _fetch_semantic_scholar(paper_id: str) -> dict:
    """
    Semantic Scholar API から論文の引用数・著者情報を取得する。
    APIキー不要・無料。失敗時は空 dict を返す（スキップ設計）。

    Args:
        paper_id: ArXiv ID（例: "2301.07041"）または SS の paperId

    Returns:
        {
          "citation_count":   int,
          "influential_count": int,   # 影響力のある引用数
          "authors":          [{name, hIndex, affiliations}],
          "year":             int,
          "is_open_access":   bool,
        }
    """
    if paper_id in _SS_CACHE:
        return _SS_CACHE[paper_id]

    try:
        import requests
        # ArXiv ID から SS paperId を構築
        ss_id = f"ARXIV:{paper_id}" if not paper_id.startswith("ARXIV:") else paper_id
        url   = f"https://api.semanticscholar.org/graph/v1/paper/{ss_id}"
        params = {
            "fields": "citationCount,influentialCitationCount,authors.hIndex,authors.name,authors.affiliations,year,isOpenAccess"
        }
        resp = requests.get(url, params=params, timeout=5,
                            headers={"User-Agent": "stock_analyzer/1.0"})
        if resp.status_code == 200:
            data = resp.json()
            result = {
                "citation_count":    data.get("citationCount", 0) or 0,
                "influential_count": data.get("influentialCitationCount", 0) or 0,
                "authors":           data.get("authors", []),
                "year":              data.get("year"),
                "is_open_access":    data.get("isOpenAccess", False),
            }
            _SS_CACHE[paper_id] = result
            return result
    except Exception:
        pass
    _SS_CACHE[paper_id] = {}
    return {}


def _calc_paper_weight(paper: dict, ss_data: dict) -> float:
    """
    1論文の重みスコアを計算する。

    重み = キーワードマッチ点数（呼び出し元で適用）
         × 引用数スコア
         × 著者スコア（機関・H-index）
         × カテゴリスコア
         × 再現性スコア（コード公開）
         × ホット度スコア（発表後72h以内の被引用スコア）

    Returns:
        weight_multiplier: float（1.0 = 標準）
    """
    weight = 1.0

    # ── 引用数スコア ──────────────────────────────────────────────
    citations = ss_data.get("citation_count", 0) or 0
    if citations >= 100:
        weight *= 3.0
    elif citations >= 50:
        weight *= 2.0
    elif citations >= 20:
        weight *= 1.5
    elif citations >= 5:
        weight *= 1.2
    # 0件はそのまま（新着論文は引用なしが正常）

    # ── 著者スコア（H-index × 機関名マッチ） ──────────────────────
    top_institutions = [
        "google", "deepmind", "openai", "mit", "stanford", "cmu",
        "microsoft research", "meta", "amazon", "berkeley", "oxford",
        "cambridge", "harvard", "eth", "tokyo", "kyoto", "nvidia",
        "anthropic", "hugging face", "brain", "fair",
    ]
    authors = ss_data.get("authors", [])
    max_h_index = 0
    has_top_affil = False

    for author in authors[:5]:   # 先頭5著者のみ確認
        h_idx = author.get("hIndex") or 0
        if h_idx > max_h_index:
            max_h_index = h_idx

        affiliations = author.get("affiliations", []) or []
        for aff in affiliations:
            aff_lower = str(aff).lower()
            if any(inst in aff_lower for inst in top_institutions):
                has_top_affil = True
                break

    if max_h_index >= 50:
        weight *= 2.0
    elif max_h_index >= 20:
        weight *= 1.5
    elif max_h_index >= 10:
        weight *= 1.2

    if has_top_affil:
        weight *= 1.5

    # ── カテゴリスコア ─────────────────────────────────────────────
    arxiv_id = paper.get("id", "") or paper.get("url", "")
    # ArXiv カテゴリを URL から推定
    categories = paper.get("categories", []) or []
    cat_str = " ".join(str(c) for c in categories).lower() + arxiv_id.lower()

    if "cs.lg" in cat_str or "machine learning" in cat_str:
        weight *= 1.3
    elif "cs.cl" in cat_str or "natural language" in cat_str:
        weight *= 1.2
    elif "econ.gn" in cat_str or "q-fin" in cat_str:
        weight *= 1.4   # 金融系は特に重要
    elif "cs.cv" in cat_str or "computer vision" in cat_str:
        weight *= 1.1

    # ── 再現性スコア（コード公開） ─────────────────────────────────
    title   = (paper.get("title",   "") or "").lower()
    summary = (paper.get("summary", "") or "").lower()
    text    = title + " " + summary
    if any(kw in text for kw in ["code available", "open source", "github.com",
                                   "open-source", "code release"]):
        weight *= 1.3

    # ── ホット度スコア（投稿後72時間以内 + 被引用あり） ──────────────
    pub_str = paper.get("published") or paper.get("pub_date") or ""
    try:
        from datetime import datetime as _dt
        pub_dt = _dt.strptime(pub_str[:10], "%Y-%m-%d")
        hours_since = (datetime.now() - pub_dt).total_seconds() / 3600
        if hours_since <= 72 and citations >= 1:
            weight *= 1.5   # 直後に被引用 = バズ論文
    except Exception:
        pass

    return round(weight, 3)


# ArXiv論文の持ち越しキャッシュ
# 2026-09-12: etf_tracker は **日次**、arxiv_monitor は **週次** なのに、
# 論文データを results["arxiv"]（その実行のメモリ上）からしか受け取って
# いなかった。そのため月曜以外は arxiv_result が None になり、
# 全8テーマが中立の 50 に落ちていた（実測 2026-09-07 月曜は
# 100/50/100/50/50/100/100 と動き、他の曜日は一律50）。
# 論文は「直近4週 vs その前4週」で比較する性質上、直近の週次取得分を
# 持ち越して使うのが正しい。取得できた日に保存し、無い日は読み出す。
_ARXIV_CACHE_FILE = "arxiv_papers_cache.json"
# 何日前までの取得分を使うか。arxiv は週次なので1回飛んでも耐える2週間。
# これより古いと「直近4週」バケットが空になり比較が壊れる。
_ARXIV_CACHE_MAX_AGE_DAYS = 14


def _arxiv_cache_path(cfg) -> Path:
    base = Path(getattr(cfg, "CACHE_DIR", "stock_data/cache")) if cfg \
        else Path("stock_data/cache")
    return base / _ARXIV_CACHE_FILE


def _extract_papers(arxiv_result) -> list:
    """arxiv_result から論文リストを取り出す（キー名の揺れを吸収）。"""
    if not isinstance(arxiv_result, dict):
        return []
    # 2026-09-14: 既読を除いた "papers"（新着のみ）を先に採っていたが、
    # トレンドは「直近4週 vs その前4週」の比較なので母集団は全件が必要。
    # 既読除外は窓ごとに不均一に件数を削り、9/14 は量子=0 / AI=11 のような
    # 極端値を出していた。全件（all_papers）を優先する。
    for key in ("all_papers", "papers", "new_papers_list"):
        v = arxiv_result.get(key)
        if isinstance(v, list) and v:
            return v
    return []


def _save_arxiv_papers(cfg, papers: list) -> None:
    """週次で取れた論文を持ち越し用に保存する。"""
    if not papers:
        return
    try:
        p = _arxiv_cache_path(cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "papers": papers,
        }, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"    ⚠️  ArXiv論文キャッシュ保存失敗: {str(e)[:60]}")


def _load_arxiv_papers(cfg) -> list:
    """持ち越したArXiv論文を読む。古すぎる場合は使わない。"""
    try:
        p = _arxiv_cache_path(cfg)
        if not p.exists():
            return []
        data = json.loads(p.read_text(encoding="utf-8"))
        saved = datetime.strptime(str(data.get("saved_at", ""))[:19],
                                  "%Y-%m-%d %H:%M:%S")
        age = (datetime.now() - saved).days
        if age > _ARXIV_CACHE_MAX_AGE_DAYS:
            print(f"    ⚠️  ArXiv論文キャッシュが{age}日前 → 使用しない")
            return []
        papers = data.get("papers")
        return papers if isinstance(papers, list) else []
    except Exception:
        return []


def _calc_arxiv_trend_score(theme: dict, arxiv_result: Optional[dict],
                             enable_semantic_scholar: bool = False) -> tuple:
    """
    ArXivの論文数増加率からトレンドスコア(0-100)を算出する。
    [⑤] Semantic Scholar APIによる重み付けをオプションで追加。

    Returns:
        (score: float, detail: dict)
    """
    empty_detail = {"recent_4w": 0, "prev_4w": 0, "growth_rate": None}

    if not arxiv_result or not isinstance(arxiv_result, dict):
        return 50.0, empty_detail

    papers = _extract_papers(arxiv_result)

    if not papers:
        return 50.0, empty_detail

    keywords = [kw.lower() for kw in theme.get("arxiv_keywords", [])]
    now       = datetime.now()
    cutoff_4w = now - timedelta(weeks=4)
    cutoff_8w = now - timedelta(weeks=8)

    recent_score = 0.0   # 重み付き合計（単純カウントから変更）
    prev_score   = 0.0
    recent_count = 0
    prev_count   = 0

    for p in papers:
        title   = (p.get("title",   "") or "").lower()
        summary = (p.get("summary", "") or "").lower()
        text    = title + " " + summary

        if not any(kw in text for kw in keywords):
            continue

        pub_str = p.get("published") or p.get("pub_date") or ""
        try:
            pub_dt = datetime.strptime(pub_str[:10], "%Y-%m-%d")
        except (ValueError, TypeError):
            continue

        # 重みの計算
        paper_weight = 1.0
        if enable_semantic_scholar:
            # ArXiv ID を抽出（URL形式: https://arxiv.org/abs/2301.07041）
            arxiv_url = p.get("url", "") or p.get("id", "")
            arxiv_id  = ""
            if "arxiv.org/abs/" in arxiv_url:
                arxiv_id = arxiv_url.split("arxiv.org/abs/")[-1].split("v")[0]
            if arxiv_id:
                ss_data = _fetch_semantic_scholar(arxiv_id)
                paper_weight = _calc_paper_weight(p, ss_data)
                time.sleep(0.1)   # SS API レート制限（100req/5min = 1.2req/s）

        if pub_dt >= cutoff_4w:
            recent_score += paper_weight
            recent_count += 1
        elif pub_dt >= cutoff_8w:
            prev_score   += paper_weight
            prev_count   += 1

    if recent_score == 0 and prev_score == 0:
        return 50.0, {"recent_4w": recent_count, "prev_4w": prev_count, "growth_rate": None}

    if prev_score < 0.1:
        growth_rate = 100.0
    else:
        growth_rate = (recent_score - prev_score) / prev_score * 100

    score = max(0.0, min(100.0, (growth_rate + 50) / 150 * 100))

    return round(score, 1), {
        "recent_4w":         recent_count,
        "prev_4w":           prev_count,
        "growth_rate":       round(growth_rate, 1),
        "recent_weighted":   round(recent_score, 2),
        "prev_weighted":     round(prev_score, 2),
        "ss_enabled":        enable_semantic_scholar,
    }


# ==============================================================
# BLOCK 3: テーマ単体スコアリング
# ==============================================================

# ==============================================================
# arXiv 検索APIでテーマごとの論文数を直接数える（2026-09-29）
# 以前は週次の ArXiv 監視が取った約150件（分野が偏る）をキーワードで数えており、
# 8テーマ中6テーマが「該当なし＝固定50点」、1テーマが0点だった。
# 実測（9/29）: 量子計算 直近4週741件←前4週592件・原子力13←6・サイバー149←124
# ==============================================================
_ARXIV_API = "https://export.arxiv.org/api/query"
_ARXIV_THEME_CACHE = "arxiv_theme_counts.json"
_ARXIV_LAG_DAYS = 3        # 直近数日は登録が遅れるので集計から外す
_ARXIV_SMOOTH = 5          # 少数件の伸び率が極端にならないよう分母分子に足す
_ARXIV_MIN_SAMPLE = 40     # 直近+前期の合計がこれ未満なら点数を中立へ寄せる（2026-10-01）


def build_arxiv_query(keywords: list, cats: list, start: str, end: str) -> str:
    """題名・要旨の語句一致 × 分野カテゴリ × 投稿日で絞る検索式（純関数）。

    2026-10-01: 旧版は all:（著者・コメント欄も含む全文）で広い語（SMR, energy storage,
    transformer 等）を引いており、ウランに AI 評価の論文、電池に AutoML の論文が混ざっていた。
    """
    kw = " OR ".join(f'ti:"{k}" OR abs:"{k}"' for k in keywords)
    q = f"({kw})"
    if cats:
        q += " AND (" + " OR ".join(f"cat:{c}" for c in cats) + ")"
    return q + f" AND submittedDate:[{start}0000 TO {end}2359]"


def _arxiv_query(keywords: list, start: str, end: str, n: int = 1, cats: list = ()):
    """(件数, [{title, url, published}]) を返す。失敗時は (None, [])。"""
    import re
    import urllib.parse
    import urllib.request
    q = build_arxiv_query(keywords, list(cats or []), start, end)
    url = _ARXIV_API + "?" + urllib.parse.urlencode({
        "search_query": q, "max_results": max(1, n),
        # 件数は並び順に依存しない。代表論文は関連度順の方がテーマに沿う（2026-10-01）
        "sortBy": "relevance", "sortOrder": "descending"})
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "stock_analyzer/1.0"})
            xml = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "ignore")
            m = re.search(r"<opensearch:totalResults[^>]*>(\d+)<", xml)
            papers = []
            for e in re.findall(r"<entry>(.*?)</entry>", xml, re.S)[:n]:
                t = re.search(r"<title>(.*?)</title>", e, re.S)
                i = re.search(r"<id>(.*?)</id>", e)
                d = re.search(r"<published>(.*?)</published>", e)
                papers.append({"title": re.sub(r"\s+", " ", t.group(1)).strip() if t else "",
                               "url": i.group(1) if i else "",
                               "published": d.group(1)[:10] if d else ""})
            return (int(m.group(1)) if m else None), papers
        except Exception:
            time.sleep(5 * (attempt + 1))
    return None, []


def arxiv_growth(recent: int, prev: int) -> float:
    """平滑化した伸び率（%）。少数件の極端な値を抑える（純関数）。"""
    return round(((recent + _ARXIV_SMOOTH) / (prev + _ARXIV_SMOOTH) - 1) * 100, 1)


def fetch_arxiv_theme_counts(cfg=None, today: Optional[datetime] = None) -> dict:
    """全テーマの直近4週・前4週の論文数と代表論文を返す（1日1回・キャッシュ）。"""
    today = today or datetime.now()
    key = today.strftime("%Y-%m-%d")
    cache_p = (Path(getattr(cfg, "CACHE_DIR", "stock_data/cache")) if cfg
               else Path("stock_data/cache")) / _ARXIV_THEME_CACHE
    try:
        c = json.loads(cache_p.read_text(encoding="utf-8"))
        if c.get("date") == key and c.get("themes"):
            return c["themes"]
    except (OSError, ValueError):
        pass
    end = today - timedelta(days=_ARXIV_LAG_DAYS)
    r0, r1 = end - timedelta(days=27), end
    p0, p1 = end - timedelta(days=55), end - timedelta(days=28)
    f = lambda d: d.strftime("%Y%m%d")
    out: dict = {}
    for th in THEME_DEFINITIONS:
        kws = th.get("arxiv_keywords", [])
        cats = th.get("arxiv_api_cats", [])
        recent, papers = _arxiv_query(kws, f(r0), f(r1), n=3, cats=cats)
        time.sleep(3.1)                      # arXiv API の利用規約（3秒に1回）
        prev, _ = _arxiv_query(kws, f(p0), f(p1), n=1, cats=cats)
        time.sleep(3.1)
        if recent is None or prev is None:
            continue
        out[th["id"]] = {"recent_4w": recent, "prev_4w": prev,
                         "growth_rate": arxiv_growth(recent, prev),
                         "papers": papers, "window": f"{r0:%m/%d}-{r1:%m/%d}"}
    if out:
        try:
            cache_p.parent.mkdir(parents=True, exist_ok=True)
            cache_p.write_text(json.dumps({"date": key, "themes": out}, ensure_ascii=False),
                               encoding="utf-8")
        except OSError:
            pass
    return out


def _score_theme(theme: dict, arxiv_result: Optional[dict], cfg=None,
                 api_counts: Optional[dict] = None) -> dict:
    """
    1テーマのETFモメンタム + ArXivトレンドを統合してスコアを返す。
    """
    etf_details = []
    for ticker in theme["etfs"]:
        data = _fetch_etf_momentum(ticker)
        if data:
            etf_details.append(data)
        time.sleep(0.3)

    etf_score    = _calc_etf_momentum_score(etf_details)
    _use_ss = getattr(cfg, "ENABLE_SEMANTIC_SCHOLAR", False) if cfg else False
    api = (api_counts or {}).get(theme["id"])
    if api:
        g = api["growth_rate"]
        raw = max(0.0, min(100.0, (g + 50) / 150 * 100))
        # 件数が少ないテーマ（ウラン・銅など）は伸び率がぶれやすいので中立(50)に寄せる
        n = int(api.get("recent_4w") or 0) + int(api.get("prev_4w") or 0)
        conf = min(1.0, n / _ARXIV_MIN_SAMPLE)
        arxiv_score = round(50 + (raw - 50) * conf, 1)
        arxiv_detail = {**api, "source": "arxiv_api", "confidence": round(conf, 2)}
    else:
        arxiv_score, arxiv_detail = _calc_arxiv_trend_score(theme, arxiv_result,
                                                              enable_semantic_scholar=_use_ss)
        arxiv_detail["source"] = "keyword_pool"

    # 熱量スコア: ETF 60% + ArXiv 40%
    heat_score = round(etf_score * 0.60 + arxiv_score * 0.40, 1)

    # シグナル判定
    if heat_score >= SIGNAL_THRESHOLDS["hot"]:
        signal = "hot"
    elif heat_score >= SIGNAL_THRESHOLDS["warming"]:
        signal = "warming"
    elif heat_score >= SIGNAL_THRESHOLDS["neutral"]:
        signal = "neutral"
    else:
        signal = "cooling"

    return {
        "theme_id":            theme["id"],
        "theme_name":          theme["name"],
        "theme_name_en":       theme["name_en"],
        "etfs":                theme["etfs"],
        "etf_momentum_score":  etf_score,
        "arxiv_trend_score":   arxiv_score,
        "heat_score":          heat_score,
        "etf_details":         etf_details,
        "arxiv_detail":        arxiv_detail,
        "signal":              signal,
    }


# ==============================================================
# BLOCK 4: レポート保存
# ==============================================================

def _save_etf_report(result: dict, cfg) -> None:
    """insights/etf/thematic_YYYY-MM-DD.md に保存する。"""
    today   = datetime.now().strftime("%Y-%m-%d")
    etf_dir = Path(getattr(cfg, "INSIGHT_DIR", "insights")) / "etf"

    SIGNAL_ICONS = {"hot": "🔥", "warming": "📈", "neutral": "➡️", "cooling": "❄️"}

    try:
        etf_dir.mkdir(parents=True, exist_ok=True)
        now   = datetime.now().strftime("%Y-%m-%d %H:%M")
        lines = [
            f"# テーマETFトラッキングレポート（{today}）",
            f"**生成日時**: {now}",
            "",
            "## 🌡️ テーマ熱量ランキング",
            "",
            "| テーマ | ETF | 熱量 | ETFモメンタム | ArXivトレンド | シグナル |",
            "|--------|-----|------|--------------|--------------|---------|",
        ]

        for t in result.get("themes", []):
            icon   = SIGNAL_ICONS.get(t["signal"], "➡️")
            etfs   = "/".join(t["etfs"])
            arxiv  = (f"+{t['arxiv_detail']['growth_rate']:.0f}%"
                      if t["arxiv_detail"].get("growth_rate") is not None
                      else "N/A")
            lines.append(
                f"| {t['theme_name']} | {etfs} | **{t['heat_score']:.0f}** "
                f"| {t['etf_momentum_score']:.0f} | {arxiv} | {icon} {t['signal']} |"
            )

        lines += ["", "## 📊 テーマ別詳細", ""]

        for t in result.get("top_themes", []):
            icon = SIGNAL_ICONS.get(t["signal"], "➡️")
            lines += [
                f"### {icon} {t['theme_name']}（熱量スコア: {t['heat_score']:.0f}）",
                "",
            ]
            for d in t["etf_details"]:
                lines.append(
                    f"- **{d['ticker']}**: ${d['price']}  "
                    f"1M:{d['ret_1m']:+.1f}%  3M:{d['ret_3m']:+.1f}%  "
                    f"MA50乖離:{d['ma50_ratio']:+.1f}%  出来高前月比:{d['vol_ratio']:+.1f}%"
                )
            ad = t["arxiv_detail"]
            if ad.get("growth_rate") is not None:
                lines.append(
                    f"- ArXiv論文: 直近4週{ad['recent_4w']}件 / "
                    f"前4週{ad['prev_4w']}件 "
                    f"（増加率: {ad['growth_rate']:+.1f}%）"
                )
                for pp in (ad.get("papers") or [])[:3]:
                    lines.append(f"  - {pp.get('title', '')}（{pp.get('published', '')}） {pp.get('url', '')}")
            lines.append("")

        out = etf_dir / f"thematic_{today}.md"
        out.write_text("\n".join(lines), encoding="utf-8")
        print(f"  💾 テーマETFレポート保存: thematic_{today}.md")

    except Exception as e:
        print(f"  ⚠️  テーマETFレポート保存失敗: {str(e)[:60]}")


# ==============================================================
# BLOCK 5: メインエントリポイント
# ==============================================================

def run_thematic_etf_tracker(cfg, arxiv_result: Optional[dict] = None) -> dict:
    """
    テーマETFトラッキングを実行する。

    Args:
        cfg          : config モジュール
        arxiv_result : results["arxiv"] の dict（週次取得済み）
                       本モジュールは日次、arxiv_monitor は週次のため、
                       渡されなかった日は前回取得分（キャッシュ）を使う。

    Returns:
        {
          "themes":      全テーマの ThemeResult リスト（heat_score 降順）,
          "top_themes":  上位 N テーマ（config.ETF_TOP_N で設定）,
          "timestamp":   str,
        }
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    print("\n  🌡️  テーマETFトラッキング（Step C'）開始...")
    t0 = datetime.now()

    top_n   = getattr(cfg, "ETF_TOP_N", 3)
    themes  = getattr(cfg, "ETF_THEME_IDS", None)   # None = 全テーマ実行

    # 2026-09-12: 本モジュールは日次、arxiv_monitor は週次。
    # 週次で取れた日は保存し、取れなかった日は前回分を読み出す。
    # これが無いと月曜以外は全テーマが ArXiv=50（中立）に固定され、
    # 熱量スコアの4割が死んだ状態になっていた。
    _fresh = _extract_papers(arxiv_result)
    if _fresh:
        _save_arxiv_papers(cfg, _fresh)
        print(f"    📚 ArXiv論文: 今回取得 {len(_fresh)}件")
    else:
        _carried = _load_arxiv_papers(cfg)
        if _carried:
            arxiv_result = {"papers": _carried}
            print(f"    📚 ArXiv論文: 前回取得分を使用 {len(_carried)}件"
                  f"（arxiv_monitor は週次のため）")
        else:
            print("    ⚠️  ArXiv論文が無いため全テーマ中立（50）で算出"
                  "（週次 arxiv_monitor の実行後に解消）")

    target_themes = THEME_DEFINITIONS
    if themes:
        target_themes = [t for t in THEME_DEFINITIONS if t["id"] in themes]

    print(f"  対象テーマ: {len(target_themes)}件")

    api_counts = {}
    if getattr(cfg, "ENABLE_ARXIV_THEME_API", True) if cfg else True:
        api_counts = fetch_arxiv_theme_counts(cfg)
        print(f"    📚 arXiv 検索API: {len(api_counts)}/{len(THEME_DEFINITIONS)}テーマの論文数を取得")

    results_list = []
    for theme in target_themes:
        print(f"  📌 {theme['name']} ({'/'.join(theme['etfs'])}) ...")
        result = _score_theme(theme, arxiv_result, cfg=cfg, api_counts=api_counts)
        results_list.append(result)
        print(
            f"    → 熱量:{result['heat_score']:.0f}  "
            f"ETF:{result['etf_momentum_score']:.0f}  "
            f"ArXiv:{result['arxiv_trend_score']:.0f}  "
            f"[{result['signal']}]"
        )

    # heat_score 降順でソート
    results_list.sort(key=lambda x: x["heat_score"], reverse=True)
    top_themes = results_list[:top_n]

    elapsed = (datetime.now() - t0).seconds
    hot_count = sum(1 for t in results_list if t["signal"] == "hot")
    print(
        f"  ✅ テーマETF完了（{elapsed}秒）: "
        f"🔥HOT:{hot_count}件 / "
        f"TOP1: {results_list[0]['theme_name']}({results_list[0]['heat_score']:.0f}点)"
        if results_list else f"  ✅ テーマETF完了（{elapsed}秒）"
    )

    output = {
        "themes":     results_list,
        "top_themes": top_themes,
        "timestamp":  datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    _save_etf_report(output, cfg)
    return output


# ==============================================================
# 単体テスト
# ==============================================================
if __name__ == "__main__":
    import importlib.util
    from pathlib import Path

    cfg = None
    # 設定ファイルの読み込み
    for p in ["/content/config.py", str(Path(__file__).parent / "config.py")]:
        path_obj = Path(p)
        if path_obj.exists():
            spec = importlib.util.spec_from_file_location("config", p)
            # spec と loader の存在を確定させ、Noneのリスクを排除
            if spec is not None and spec.loader is not None:
                cfg = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(cfg)
                break

    # cfg が正しく読み込めている場合のみ、解析処理を実行する
    if cfg is not None:
        result = run_thematic_etf_tracker(cfg, arxiv_result=None)
        
        print("\n--- 熱量ランキング ---")
        # 結果のリストを取得（万が一結果が空だった場合も考慮）
        themes = result.get("themes", [])
        for t in themes:
            # 信号に応じたアイコンの取得
            signal_map = {"hot": "🔥", "warming": "📈", "neutral": "➡️", "cooling": "❄️"}
            icon = signal_map.get(t.get("signal", "neutral"), "➡️")
            
            # スコアの表示
            heat_score = t.get("heat_score", 0)
            print(f"  {icon} {t.get('theme_name', '不明')}: {heat_score:.0f}点")
    else:
        print("ERROR: config.py が読み込めなかったため、ETFトラッカーを実行できません。")
