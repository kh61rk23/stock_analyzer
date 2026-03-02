# ==============================================================
# idea_generator.py  v1.0  (4-A)
# ArXiv × マクロ × セクター → 投資アイデア自動生成
# ==============================================================
# 【役割】
#   ① ArXiv 最新レポート（.md）から論文情報を抽出
#   ② アクティブなマクロシグナルを読み込む
#   ③ セクター分析結果（composite_score, risk_level）を読み込む
#   ④ キーワードマッチングで「論文テーマ × 追い風業種」を紐付け
#   ⑤ スコアリングして上位 N 件を「投資アイデア」としてフォーマット化
#   ⑥ INSIGHT_DIR/ideas/idea_YYYY-MM-DD.json / .md に保存
#
# 【返り値スキーマ】run_idea_generator(cfg, sector_results, macro_signals) の返り値
#   {
#     "ideas": [
#       {
#         "rank":          1,
#         "title":         "アイデアタイトル",
#         "thesis":        "投資テーゼ（1〜2文）",
#         "source_paper":  "ArXiv 論文タイトル",
#         "paper_url":     "https://arxiv.org/...",
#         "sector_code":   "1028",
#         "sector_name":   "銀行業",
#         "sector_score":  82.5,
#         "macro_signals": ["円安追い風", "金利上昇局面"],
#         "keywords":      ["LLM", "credit risk"],
#         "idea_score":    74.2,
#         "generated_at":  "2026-02-27",
#       },
#       ...
#     ],
#     "meta": {
#       "papers_scanned":  15,
#       "sectors_checked": 6,
#       "signals_active":  2,
#       "timestamp":       "2026-02-27 08:00:00",
#     }
#   }
#   失敗時は {"ideas": [], "meta": {...}} を返す
#
# 【依存】config.py / json / re / pathlib
# 【呼出】from idea_generator import run_idea_generator
# ==============================================================

import json
import re
from datetime import datetime
from pathlib import Path


# ==============================================================
# BLOCK 1: 業種キーワードマッピング
# ==============================================================
# 業種コード → 関連キーワード（英語・日本語混在）
# ArXiv 論文タイトル/要旨とのマッチングに使用
SECTOR_KEYWORDS = {
    "1028": ["bank", "banking", "credit", "lending", "loan", "fintech",
             "financial risk", "credit risk", "Basel", "金融", "銀行", "信用"],
    "1032": ["real estate", "REIT", "property", "mortgage", "housing",
             "construction", "不動産", "住宅", "建設"],
    "1017": ["automotive", "vehicle", "EV", "electric vehicle", "autonomous",
             "self-driving", "mobility", "自動車", "EV", "電気自動車"],
    "1025": ["telecom", "5G", "network", "communication", "broadband",
             "internet", "AI", "platform", "通信", "ネットワーク"],
    "1016": ["semiconductor", "chip", "AI", "machine learning", "deep learning",
             "robotics", "IoT", "electronics", "半導体", "AI", "電子"],
    "1022": ["shipping", "logistics", "supply chain", "freight", "trade",
             "海運", "物流", "サプライチェーン"],
    "1009": ["energy", "oil", "gas", "renewable", "solar", "carbon",
             "petroleum", "エネルギー", "石油", "再生可能"],
    "1004": ["food", "agriculture", "nutrition", "supply", "consumer",
             "食品", "農業", "消費"],
}

# マクロシグナルラベル → 追い風になる業種コード
SIGNAL_SECTOR_MAP = {
    "円安":     ["1017", "1022", "1016"],
    "円高":     ["1004", "1032"],
    "金利上昇": ["1028"],
    "金利低下": ["1032"],
    "原油高":   ["1009"],
    "VIX上昇":  [],    # リスクオフ（全般逆風）
    "VIX低下":  ["1016", "1017"],
}


# ==============================================================
# BLOCK 2: ArXiv レポート読み込み・解析
# ==============================================================

def load_latest_arxiv_papers(cfg) -> list:
    """
    ARXIV_DIR から最新の arxiv_report_*.md を読み込み、
    論文情報リストを返す。

    Returns:
        [{"title": str, "url": str, "abstract": str, "keywords": [str]}, ...]
    """
    arxiv_dir = Path(getattr(cfg, "ARXIV_DIR", ""))
    if not arxiv_dir.exists():
        return []

    reports = sorted(arxiv_dir.glob("arxiv_report_*.md"), reverse=True)
    if not reports:
        return []

    latest = reports[0]
    try:
        text = latest.read_text(encoding="utf-8")
    except Exception:
        return []

    papers = []
    # MD フォーマット例: "### タイトル\n**URL**: ...\n**概要**: ..."
    # 複数パターンに対応する汎用パーサー
    blocks = re.split(r"\n#{2,4} ", text)
    for block in blocks[1:]:  # 最初のブロック（ヘッダー）はスキップ
        lines = block.strip().splitlines()
        if not lines:
            continue

        title = lines[0].strip("# ").strip()

        # URL 抽出
        url = ""
        url_match = re.search(r"https?://arxiv\.org/\S+", block)
        if url_match:
            url = url_match.group(0).rstrip(")")

        # 概要テキスト抽出（**概要**: or **Abstract**: 以降）
        abstract = ""
        ab_match = re.search(r"\*\*(?:概要|Abstract|要旨)\*\*[:\s]+(.+?)(?=\n\*\*|\Z)",
                              block, re.DOTALL)
        if ab_match:
            abstract = ab_match.group(1).strip()[:500]

        # キーワード抽出（英単語 3文字以上）
        words = re.findall(r"[A-Za-z]{3,}", title + " " + abstract)
        keywords = list({w.lower() for w in words if len(w) >= 3})[:20]

        if title and len(title) > 5:
            papers.append({
                "title":    title,
                "url":      url,
                "abstract": abstract,
                "keywords": keywords,
            })

    return papers[:30]  # 最大30件


# ==============================================================
# BLOCK 3: マクロシグナル読み込み
# ==============================================================

def load_macro_signals(cfg, macro_signals_input: list) -> list:
    """
    run_all_tasks から渡されたマクロシグナルリストを整形する。
    渡されない場合は最新 macro_report_*.md から読み込む。

    Returns:
        [{"label": str, "sector_signals": {code: "tailwind"|"headwind"}}, ...]
    """
    if macro_signals_input:
        return macro_signals_input

    # フォールバック: MD ファイルから読み込む
    macro_dir = Path(getattr(cfg, "MACRO_DIR", ""))
    if not macro_dir.exists():
        return []

    reports = sorted(macro_dir.glob("macro_report_*.md"), reverse=True)
    if not reports:
        return []

    # ファイルベースの簡易読み込み（ラベルだけ抽出）
    try:
        text = reports[0].read_text(encoding="utf-8")
        labels = re.findall(r"##\s+(.+?)(?:\n|$)", text)
        return [{"label": lbl, "sector_signals": {}} for lbl in labels[:5]]
    except Exception:
        return []


# ==============================================================
# BLOCK 4: セクタースコア読み込み
# ==============================================================

def load_sector_scores(cfg, sector_results_input: dict) -> dict:
    """
    run_all_tasks から渡されたセクター分析結果を整形する。
    渡されない場合は最新 result JSON から読み込む。

    Returns:
        {sector_code: {"name": str, "score": float, "risk": str}}
    """
    if sector_results_input:
        return {
            code: {
                "name":  res.get("sector_name_ja", code),
                "score": float(res.get("composite_score", 50)),
                "risk":  res.get("risk_level", "medium"),
            }
            for code, res in sector_results_input.items()
        }

    # フォールバック: result ディレクトリから読み込む
    result_dir = Path(getattr(cfg, "RESULT_DIR", ""))
    if not result_dir.exists():
        return {}

    scores = {}
    for jf in sorted(result_dir.glob("sector_*.json"), reverse=True)[:10]:
        try:
            data = json.loads(jf.read_text(encoding="utf-8"))
            code = data.get("sector_code", "")
            if code:
                scores[code] = {
                    "name":  data.get("sector_name_ja", code),
                    "score": float(data.get("composite_score", 50)),
                    "risk":  data.get("risk_level", "medium"),
                }
        except Exception:
            continue
    return scores


# ==============================================================
# BLOCK 5: キーワードマッチング
# ==============================================================

def match_paper_to_sectors(paper: dict) -> list:
    """
    1論文に対してヒットする業種コードリストを返す。
    タイトル + キーワードを SECTOR_KEYWORDS と照合する。

    Returns:
        [(sector_code, hit_count), ...] ヒット数降順
    """
    text = (paper["title"] + " " + " ".join(paper["keywords"])).lower()
    hits = []
    for code, kws in SECTOR_KEYWORDS.items():
        count = sum(1 for kw in kws if kw.lower() in text)
        if count > 0:
            hits.append((code, count))
    hits.sort(key=lambda x: x[1], reverse=True)
    return hits


def match_signal_to_sectors(signal_label: str) -> list:
    """
    マクロシグナルラベルから追い風業種コードを返す。
    部分一致で検索する。

    Returns:
        [sector_code, ...]
    """
    for key, codes in SIGNAL_SECTOR_MAP.items():
        if key in signal_label:
            return codes
    return []


# ==============================================================
# BLOCK 6: アイデアスコアリング
# ==============================================================

def calc_idea_score(
    kw_hits:       int,   # キーワードマッチ数（0〜10）
    sector_score:  float, # セクター composite_score（0〜100）
    macro_overlap: int,   # マクロシグナルとの一致数（0〜5）
    risk_level:    str,   # "low"|"medium"|"high"
) -> float:
    """
    投資アイデアのスコアを計算する。

    スコア式:
        base = sector_score × 0.5
             + kw_hits    × 10 (最大 50)
             + macro_overlap × 8 (最大 40)
        risk_penalty = {"low": 0, "medium": -5, "high": -15}
        final = min(100, base - risk_penalty)
    """
    risk_penalty = {"low": 0, "medium": -5, "high": -15}.get(risk_level, -5)
    base = (
        sector_score * 0.5 +
        min(kw_hits, 5) * 10 +
        min(macro_overlap, 5) * 8
    )
    return round(min(100.0, max(0.0, base + risk_penalty)), 1)


# ==============================================================
# BLOCK 7: アイデア生成メイン
# ==============================================================

def generate_ideas(
    papers:         list,
    sector_scores:  dict,
    macro_signals:  list,
    cfg,
) -> list:
    """
    論文 × 業種 × マクロシグナルを照合してアイデアリストを生成する。

    Returns:
        [idea_dict, ...]  idea_score 降順・上位 IDEA_TOP_N 件
    """
    top_n     = getattr(cfg, "IDEA_TOP_N", 5)
    threshold = getattr(cfg, "IDEA_KW_THRESHOLD", 1)
    today     = datetime.now().strftime("%Y-%m-%d")

    # アクティブなマクロシグナルから追い風業種を収集
    signal_tailwind = {}   # {sector_code: [signal_label, ...]}
    for sig in macro_signals:
        label = sig.get("label", "")
        # sector_signals から tailwind のコードを取得
        for code, status in sig.get("sector_signals", {}).items():
            if status == "tailwind":
                signal_tailwind.setdefault(code, []).append(label)
        # ラベルベースのマッピングも併用
        for code in match_signal_to_sectors(label):
            signal_tailwind.setdefault(code, []).append(label)

    ideas = []
    for paper in papers:
        sector_hits = match_paper_to_sectors(paper)
        for code, kw_hits in sector_hits:
            if kw_hits < threshold:
                continue
            if code not in sector_scores:
                continue

            sec  = sector_scores[code]
            m_signals = signal_tailwind.get(code, [])
            score = calc_idea_score(
                kw_hits      = kw_hits,
                sector_score = sec["score"],
                macro_overlap= len(m_signals),
                risk_level   = sec["risk"],
            )

            # 投資テーゼ文を自動生成
            thesis = _build_thesis(paper["title"], sec["name"], m_signals)

            ideas.append({
                "title":         f"{sec['name']} × {paper['title'][:40]}",
                "thesis":        thesis,
                "source_paper":  paper["title"],
                "paper_url":     paper.get("url", ""),
                "sector_code":   code,
                "sector_name":   sec["name"],
                "sector_score":  sec["score"],
                "macro_signals": m_signals,
                "keywords":      paper["keywords"][:8],
                "idea_score":    score,
                "generated_at":  today,
            })

    # スコア降順・重複排除（同業種からは最高スコアのみ）
    seen_sectors = set()
    deduped      = []
    for idea in sorted(ideas, key=lambda x: x["idea_score"], reverse=True):
        if idea["sector_code"] not in seen_sectors:
            deduped.append(idea)
            seen_sectors.add(idea["sector_code"])

    ranked = deduped[:top_n]
    for i, idea in enumerate(ranked, 1):
        idea["rank"] = i

    return ranked


def _build_thesis(paper_title: str, sector_name: str, signals: list) -> str:
    """論文タイトルと業種・マクロシグナルから投資テーゼ文を生成する。"""
    signal_str = "・".join(signals) if signals else "独自テーマ"
    return (
        f"「{paper_title[:35]}」の技術的知見が"
        f"【{sector_name}】に応用できる可能性がある。"
        f"現在のマクロ環境（{signal_str}）と合致しており、"
        f"中期的な投資機会として検討に値する。"
    )


# ==============================================================
# BLOCK 8: メインエントリポイント
# ==============================================================

def run_idea_generator(
    cfg,
    sector_results: dict = None,
    macro_signals:  list = None,
) -> dict:
    """
    投資アイデアの自動生成を実行する。
    失敗時は {"ideas": [], "meta": {...}} を返す。

    Args:
        cfg            : config モジュール
        sector_results : run_all_tasks の results["sectors"]（省略可）
        macro_signals  : evaluate_macro_signals の返り値（省略可）
    """
    print("\n  💡 アイデアジェネレーター 開始...")
    t0 = datetime.now()

    # 入力データ収集
    papers        = load_latest_arxiv_papers(cfg)
    sec_scores    = load_sector_scores(cfg, sector_results or {})
    mac_signals   = load_macro_signals(cfg, macro_signals or [])

    print(f"    論文: {len(papers)}件 / 業種: {len(sec_scores)}件 / "
          f"シグナル: {len(mac_signals)}件")

    if not papers:
        print("    ⚠️  ArXiv 論文が見つかりません（arxiv_report_*.md を確認）")
        return {
            "ideas": [],
            "meta": {
                "papers_scanned":  0,
                "sectors_checked": len(sec_scores),
                "signals_active":  len(mac_signals),
                "timestamp":       datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "warning":         "ArXiv レポートが未生成です",
            }
        }

    # アイデア生成
    ideas = generate_ideas(papers, sec_scores, mac_signals, cfg)
    elapsed = (datetime.now() - t0).seconds

    print(f"    ✅ アイデア生成完了: {len(ideas)}件（{elapsed}秒）")
    for idea in ideas:
        print(f"      #{idea['rank']} [{idea['sector_name']}] "
              f"スコア:{idea['idea_score']} — {idea['title'][:50]}")

    meta = {
        "papers_scanned":  len(papers),
        "sectors_checked": len(sec_scores),
        "signals_active":  len(mac_signals),
        "timestamp":       datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    result = {"ideas": ideas, "meta": meta}
    _save_idea_report(result, cfg)
    return result


# ==============================================================
# BLOCK 9: 保存処理
# ==============================================================

def _save_idea_report(result: dict, cfg) -> None:
    """
    INSIGHT_DIR/ideas/idea_YYYY-MM-DD.json + .md に保存する。
    """
    today     = datetime.now().strftime("%Y-%m-%d")
    insight_dir = Path(getattr(cfg, "INSIGHT_DIR", "insights"))
    idea_dir  = insight_dir / "ideas"

    try:
        idea_dir.mkdir(parents=True, exist_ok=True)

        # JSON
        json_path = idea_dir / f"idea_{today}.json"
        json_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

        # Markdown
        ideas = result.get("ideas", [])
        meta  = result.get("meta", {})
        lines = [
            f"# 投資アイデア自動生成レポート（{today}）", "",
            f"**生成アイデア数**: {len(ideas)}件 / "
            f"**論文スキャン数**: {meta.get('papers_scanned', 0)}件 / "
            f"**アクティブシグナル**: {meta.get('signals_active', 0)}件",
            "",
        ]
        for idea in ideas:
            lines += [
                f"## #{idea['rank']} {idea['title']}",
                "",
                f"**スコア**: {idea['idea_score']} / 100",
                f"**業種**: {idea['sector_name']}（スコア: {idea['sector_score']}）",
                f"**投資テーゼ**: {idea['thesis']}",
                f"**参照論文**: [{idea['source_paper'][:60]}]({idea['paper_url']})",
                f"**マクロシグナル**: {' / '.join(idea['macro_signals']) or 'なし'}",
                f"**キーワード**: {', '.join(idea['keywords'][:6])}",
                "",
            ]

        md_path = idea_dir / f"idea_{today}.md"
        md_path.write_text("\n".join(lines), encoding="utf-8")

        print(f"    💾 アイデアレポート保存: idea_{today}.json / .md")

    except Exception as e:
        print(f"    ⚠️  アイデアレポート保存失敗: {str(e)[:60]}")


def load_latest_ideas(cfg) -> dict:
    """
    UI 表示用: 最新の idea_*.json を読み込んで返す。
    見つからない場合は {} を返す。
    """
    insight_dir = Path(getattr(cfg, "INSIGHT_DIR", "insights"))
    idea_dir    = insight_dir / "ideas"
    if not idea_dir.exists():
        return {}
    files = sorted(idea_dir.glob("idea_*.json"), reverse=True)
    if not files:
        return {}
    try:
        return json.loads(files[0].read_text(encoding="utf-8"))
    except Exception:
        return {}


# 単体テスト
if __name__ == "__main__":
    import importlib.util
    for p in ["/content/config.py", str(Path(__file__).parent / "config.py")]:
        if Path(p).exists():
            spec = importlib.util.spec_from_file_location("config", p)
            cfg  = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cfg)
            break
    result = run_idea_generator(cfg)
    print(f"\nアイデア数: {len(result.get('ideas', []))}")
    for idea in result.get("ideas", []):
        print(f"  #{idea['rank']} [{idea['idea_score']}] {idea['title']}")
