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
            "quantum computing", "quantum algorithm", "qubit",
            "quantum supremacy", "quantum error correction",
        ],
        "arxiv_categories": ["quant-ph", "cs.ET"],
    },
    {
        "id":       "uranium",
        "name":     "ウラン・原子力",
        "name_en":  "Uranium & Nuclear",
        "etfs":     ["URA", "NLR"],
        "arxiv_keywords": [
            "nuclear energy", "uranium", "small modular reactor", "SMR",
            "nuclear fusion", "fission reactor",
        ],
        "arxiv_categories": ["physics.app-ph"],
    },
    {
        "id":       "lithium",
        "name":     "リチウム・電池",
        "name_en":  "Lithium & Battery",
        "etfs":     ["LIT"],
        "arxiv_keywords": [
            "lithium battery", "lithium ion", "solid state battery",
            "battery technology", "energy storage",
        ],
        "arxiv_categories": ["cond-mat.mtrl-sci"],
    },
    {
        "id":       "copper_metals",
        "name":     "銅・希少金属",
        "name_en":  "Copper & Rare Metals",
        "etfs":     ["COPX", "REMX"],
        "arxiv_keywords": [
            "copper demand", "rare earth", "critical minerals",
            "electric vehicle materials", "battery metals",
        ],
        "arxiv_categories": ["cond-mat.mtrl-sci"],
    },
    {
        "id":       "space",
        "name":     "宇宙・航空",
        "name_en":  "Space & Aerospace",
        "etfs":     ["UFO", "ARKX"],
        "arxiv_keywords": [
            "satellite", "space exploration", "rocket propulsion",
            "space economy", "low earth orbit",
        ],
        "arxiv_categories": ["astro-ph.IM"],
    },
    {
        "id":       "cybersecurity",
        "name":     "サイバーセキュリティ",
        "name_en":  "Cybersecurity",
        "etfs":     ["CIBR"],
        "arxiv_keywords": [
            "cybersecurity", "cyber attack", "intrusion detection",
            "ransomware", "zero-day", "adversarial attack",
        ],
        "arxiv_categories": ["cs.CR"],
    },
    {
        "id":       "ai_infra",
        "name":     "AI・ロボティクス",
        "name_en":  "AI & Robotics",
        "etfs":     ["BOTZ"],
        "arxiv_keywords": [
            "large language model", "transformer", "deep learning",
            "generative AI", "reinforcement learning", "robotics",
        ],
        "arxiv_categories": ["cs.AI", "cs.LG", "cs.RO"],
    },
    {
        "id":       "hydrogen",
        "name":     "水素・クリーンエネルギー",
        "name_en":  "Hydrogen & Clean Energy",
        "etfs":     ["ICLN"],
        "arxiv_keywords": [
            "hydrogen energy", "fuel cell", "green hydrogen",
            "electrolysis", "renewable energy storage",
        ],
        "arxiv_categories": ["cond-mat.mtrl-sci", "physics.app-ph"],
    },
]

# 熱量シグナルの閾値
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
        import yfinance as yf # type: ignore # yfinance は外部依存のため、実行環境にない場合はこのモジュール全体をスキップする設計

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

    papers = []
    for key in ("papers", "new_papers_list", "all_papers"):
        v = arxiv_result.get(key)
        if isinstance(v, list):
            papers = v
            break

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

def _score_theme(theme: dict, arxiv_result: Optional[dict], cfg=None) -> dict:
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
    arxiv_score, arxiv_detail = _calc_arxiv_trend_score(theme, arxiv_result,
                                                          enable_semantic_scholar=_use_ss)

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
                       None の場合は ArXivスコアを 50（中立）で代替

    Returns:
        {
          "themes":      全テーマの ThemeResult リスト（heat_score 降順）,
          "top_themes":  上位 N テーマ（config.ETF_TOP_N で設定）,
          "timestamp":   str,
        }
    """
    print("\n  🌡️  テーマETFトラッキング（Step C'）開始...")
    t0 = datetime.now()

    top_n   = getattr(cfg, "ETF_TOP_N", 3)
    themes  = getattr(cfg, "ETF_THEME_IDS", None)   # None = 全テーマ実行

    target_themes = THEME_DEFINITIONS
    if themes:
        target_themes = [t for t in THEME_DEFINITIONS if t["id"] in themes]

    print(f"  対象テーマ: {len(target_themes)}件")

    results_list = []
    for theme in target_themes:
        print(f"  📌 {theme['name']} ({'/'.join(theme['etfs'])}) ...")
        result = _score_theme(theme, arxiv_result, cfg=cfg)
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
