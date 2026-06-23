# ==============================================================
# academic_conf_tracker.py  v1.0  (Wave 7 - Step 2)
# 学術カンファレンス発表追跡モジュール
# ==============================================================
# 【役割】
#   IEEE ISSCC / IEDM / VLSI / 応用物理学会 / NeurIPS / ICLR 等の
#   発表プログラム（PDFテキスト・HTMLアブストラクト）から
#   所属企業を抽出し、企業別の研究開発投資シグナルを生成する。
#
# 【設計思想】
#   ・学術発表の増加 = 量産の2〜4年前の先行R&D投資の証左
#   ・自社所属著者が増加している企業 → 当該技術領域への本格投資開始
#   ・ラボ/大学との共著 → 外部連携によるオープンイノベーション度合い
#   ・発表テーマと taxonomy.taxonomy_node の対応付けで
#     technology_cluster_engine.py のクラスタースコアを補強する
#
# 【主要出力キャッシュ】
#   CACHE_DIR/academic_conf_cache.json
#   TTL: 180日（月次更新）
#   スキーマ:
#     {
#       "last_updated": "YYYY-MM-DD",
#       "version": "1.0",
#       "conferences": {
#         "isscc_2025": {
#           "conf_id": str,
#           "name": str,
#           "year": int,
#           "field": str,           # "semiconductor" / "ai_ml" / "physics"
#           "tech_node_ids": list,  # taxonomy_node.node_id と対応
#           "papers": [
#             {
#               "title": str,
#               "authors": list,
#               "affiliations": list,  # 生テキスト
#               "companies_detected": list,  # {ticker, code, name}
#               "universities_detected": list,
#               "topic_tags": list,
#               "session": str,
#             }
#           ],
#           "company_paper_counts": {ticker: int},
#           "total_papers": int,
#           "signal_score": float,
#         }
#       },
#       "company_scores": {
#         ticker: {
#           "academic_score": float,   # 0.0 ~ 100.0
#           "total_papers": int,
#           "leading_confs": list,
#           "university_collab_ratio": float,
#           "yoy_paper_delta": float,
#         }
#       },
#       "stats": { ... }
#     }
#
# 【データ取得戦略】
#   Level 1: 学会公式プログラムページ（HTML解析）
#   Level 2: arXiv preprint（学会投稿前の事前公開論文）
#   Level 3: Semantic Scholar API（引用・所属情報）
#   Level 4: キャッシュ継続使用
#
# 【依存】
#   config.py（Wave 7追加定数参照）
#   requests, BeautifulSoup（外部ライブラリ）
#   ★ try/except で全ブロックを保護
# ==============================================================

from __future__ import annotations

import json
import time
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

# ==============================================================
# BLOCK 0: デフォルト定数
# ==============================================================

_CACHE_FILENAME = "academic_conf_cache.json"
_DEFAULT_TTL_DAYS = 180

# 追跡対象カンファレンス定義
_DEFAULT_CONF_CONFIG: dict = {
    "isscc": {
        "name":          "IEEE ISSCC",
        "full_name":     "International Solid-State Circuits Conference",
        "field":         "semiconductor",
        "years":         [2024, 2025],
        "tech_node_ids": [
            "semiconductor_logic", "semiconductor_memory",
            "analog_ic", "power_ic", "semiconductor_rf",
        ],
        "program_urls": {
            2025: "https://isscc.org/program/",
            2024: "https://isscc.org/2024/program/",
        },
        "arxiv_categories": ["eess.SP", "cs.AR"],
        "arxiv_keywords":   ["CMOS", "SRAM", "ADC", "DAC", "mmWave", "ISSCC"],
        "known_contributors_jp": {
            "Sony":      {"ticker": "6758.T", "code": "6758"},
            "Toshiba":   {"ticker": "6502.T", "code": "6502"},
            "Renesas":   {"ticker": "6723.T", "code": "6723"},
            "Rohm":      {"ticker": "6963.T", "code": "6963"},
            "Murata":    {"ticker": "6981.T", "code": "6981"},
            "Fujitsu":   {"ticker": "6702.T", "code": "6702"},
            "Hitachi":   {"ticker": "6501.T", "code": "6501"},
            "Toppan":    {"ticker": "7911.T", "code": "7911"},
            "Kioxia":    {"ticker": None,      "code": "KIOXIA"},
        },
        "known_contributors_us": {
            "Intel":     {"ticker": "INTC",  "code": "INTC"},
            "Qualcomm":  {"ticker": "QCOM",  "code": "QCOM"},
            "Apple":     {"ticker": "AAPL",  "code": "AAPL"},
            "Nvidia":    {"ticker": "NVDA",  "code": "NVDA"},
            "AMD":       {"ticker": "AMD",   "code": "AMD"},
            "Broadcom":  {"ticker": "AVGO",  "code": "AVGO"},
            "Marvell":   {"ticker": "MRVL",  "code": "MRVL"},
            "Samsung":   {"ticker": None,    "code": "SSNLF"},
            "TSMC":      {"ticker": "TSM",   "code": "TSM"},
        },
        "known_universities": [
            "MIT", "Stanford", "Berkeley", "Caltech",
            "Tokyo University", "Kyoto University", "Tohoku University",
            "TU Delft", "ETH Zurich", "KAIST",
        ],
    },
    "iedm": {
        "name":          "IEEE IEDM",
        "full_name":     "International Electron Devices Meeting",
        "field":         "semiconductor",
        "years":         [2024, 2025],
        "tech_node_ids": [
            "semiconductor_logic", "semiconductor_memory",
            "power_semiconductor", "sensor_image",
        ],
        "program_urls": {
            2025: "https://ieee-iedm.org/program/",
            2024: "https://ieee-iedm.org/2024/program/",
        },
        "arxiv_categories": ["cond-mat.mes-hall", "physics.app-ph"],
        "arxiv_keywords":   ["FinFET", "GAA", "DRAM", "NAND", "IEDM", "transistor"],
        "known_contributors_jp": {
            "Kioxia":    {"ticker": None,      "code": "KIOXIA"},
            "Sony":      {"ticker": "6758.T",  "code": "6758"},
            "Renesas":   {"ticker": "6723.T",  "code": "6723"},
            "Rohm":      {"ticker": "6963.T",  "code": "6963"},
            "Shin-Etsu": {"ticker": "4063.T",  "code": "4063"},
            "Sumco":     {"ticker": "3436.T",  "code": "3436"},
        },
        "known_contributors_us": {
            "Intel":     {"ticker": "INTC",   "code": "INTC"},
            "Micron":    {"ticker": "MU",     "code": "MU"},
            "Applied Materials": {"ticker": "AMAT", "code": "AMAT"},
            "Lam Research":      {"ticker": "LRCX", "code": "LRCX"},
            "TSMC":      {"ticker": "TSM",    "code": "TSM"},
            "Samsung":   {"ticker": None,     "code": "SSNLF"},
        },
        "known_universities": [
            "MIT", "Stanford", "Tokyo University", "Tohoku University",
            "Nagoya University", "KAIST", "National Taiwan University",
        ],
    },
    "neurips": {
        "name":          "NeurIPS",
        "full_name":     "Conference on Neural Information Processing Systems",
        "field":         "ai_ml",
        "years":         [2024, 2025],
        "tech_node_ids": [
            "ai_ml_framework", "ai_edge_chip",
            "datacenter_ai_chip", "ai_software",
        ],
        "program_urls": {
            2025: "https://neurips.cc/Conferences/2025/Schedule",
            2024: "https://neurips.cc/Conferences/2024/Schedule",
        },
        "arxiv_categories": ["cs.LG", "cs.AI", "stat.ML"],
        "arxiv_keywords":   [
            "large language model", "diffusion model",
            "transformer", "reinforcement learning", "NeurIPS",
        ],
        "known_contributors_jp": {
            "Sony":       {"ticker": "6758.T", "code": "6758"},
            "Fujitsu":    {"ticker": "6702.T", "code": "6702"},
            "NEC":        {"ticker": "6701.T", "code": "6701"},
            "Hitachi":    {"ticker": "6501.T", "code": "6501"},
            "NTT":        {"ticker": "9432.T", "code": "9432"},
            "Preferred Networks": {"ticker": None, "code": "PFN"},
        },
        "known_contributors_us": {
            "Google":     {"ticker": "GOOGL", "code": "GOOGL"},
            "Microsoft":  {"ticker": "MSFT",  "code": "MSFT"},
            "Meta":       {"ticker": "META",  "code": "META"},
            "Nvidia":     {"ticker": "NVDA",  "code": "NVDA"},
            "Apple":      {"ticker": "AAPL",  "code": "AAPL"},
            "Amazon":     {"ticker": "AMZN",  "code": "AMZN"},
            "OpenAI":     {"ticker": None,    "code": "OPENAI"},
            "Anthropic":  {"ticker": None,    "code": "ANTHROPIC"},
            "DeepMind":   {"ticker": "GOOGL", "code": "GOOGL"},
        },
        "known_universities": [
            "MIT", "Stanford", "Berkeley", "CMU", "Oxford",
            "Tokyo University", "Kyoto University", "Osaka University",
        ],
    },
    "iclr": {
        "name":          "ICLR",
        "full_name":     "International Conference on Learning Representations",
        "field":         "ai_ml",
        "years":         [2025],
        "tech_node_ids": ["ai_ml_framework", "ai_software"],
        "program_urls": {
            2025: "https://iclr.cc/virtual/2025/papers.html",
        },
        "arxiv_categories": ["cs.LG", "cs.CL"],
        "arxiv_keywords":   ["ICLR", "representation learning", "LLM", "fine-tuning"],
        "known_contributors_jp": {
            "Sony":       {"ticker": "6758.T", "code": "6758"},
            "Fujitsu":    {"ticker": "6702.T", "code": "6702"},
            "NEC":        {"ticker": "6701.T", "code": "6701"},
        },
        "known_contributors_us": {
            "Google":     {"ticker": "GOOGL", "code": "GOOGL"},
            "Microsoft":  {"ticker": "MSFT",  "code": "MSFT"},
            "Meta":       {"ticker": "META",  "code": "META"},
            "Nvidia":     {"ticker": "NVDA",  "code": "NVDA"},
        },
        "known_universities": [
            "MIT", "Stanford", "Berkeley", "CMU", "Oxford", "ETH Zurich",
        ],
    },
    "applied_physics_jp": {
        "name":          "応用物理学会",
        "full_name":     "The Japan Society of Applied Physics",
        "field":         "physics",
        "years":         [2024, 2025],
        "tech_node_ids": [
            "power_semiconductor", "sensor_image",
            "semiconductor_logic", "photonics",
        ],
        "program_urls": {
            2025: "https://meeting.jsap.or.jp/",
            2024: "https://meeting.jsap.or.jp/2024/",
        },
        "arxiv_categories": ["physics.app-ph", "cond-mat.mtrl-sci"],
        "arxiv_keywords":   ["SiC", "GaN", "perovskite", "photovoltaic", "JSAP"],
        "known_contributors_jp": {
            "Rohm":       {"ticker": "6963.T", "code": "6963"},
            "Sumitomo Electric": {"ticker": "5802.T", "code": "5802"},
            "Mitsubishi Electric": {"ticker": "6503.T", "code": "6503"},
            "Fuji Electric":  {"ticker": "6504.T", "code": "6504"},
            "Toyota":         {"ticker": "7203.T", "code": "7203"},
            "Denso":          {"ticker": "6902.T", "code": "6902"},
            "Shin-Etsu":      {"ticker": "4063.T", "code": "4063"},
            "Sumco":          {"ticker": "3436.T", "code": "3436"},
            "JGC Holdings":   {"ticker": "1663.T", "code": "1663"},
        },
        "known_contributors_us": {
            "Wolfspeed":  {"ticker": "WOLF",  "code": "WOLF"},
            "ON Semi":    {"ticker": "ON",    "code": "ON"},
            "STMicro":    {"ticker": "STM",   "code": "STM"},
            "Infineon":   {"ticker": "IFNNY", "code": "IFNNY"},
        },
        "known_universities": [
            "Tokyo University", "Kyoto University", "Osaka University",
            "Nagoya University", "Tohoku University", "Waseda University",
        ],
    },
}

# arXiv API エンドポイント
_ARXIV_API_URL = "http://export.arxiv.org/api/query"
_SEMANTIC_SCHOLAR_URL = "https://api.semanticscholar.org/graph/v1/paper/search"

_REQUEST_TIMEOUT = 20
_REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; StockAnalyzer/1.0; "
        "+https://github.com/user/stock-analyzer)"
    ),
    "Accept": "application/json, text/html, */*",
}

# スコア重み
_ACADEMIC_SCORE_WEIGHTS = {
    "paper_count_norm":    0.40,
    "univ_collab_ratio":   0.20,
    "yoy_growth":          0.25,
    "field_diversity":     0.15,
}
_ACADEMIC_SCORE_MAX = 100.0


# ==============================================================
# BLOCK 1: キャッシュ管理
# ==============================================================

def _cache_path(cfg) -> Path:
    d = Path(getattr(cfg, "CACHE_DIR", "stock_data/cache"))
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d / _CACHE_FILENAME


def _load_cache(cfg) -> Optional[dict]:
    try:
        p = _cache_path(cfg)
        if not p.exists():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        last_updated = data.get("last_updated", "")
        ttl_days = int(getattr(cfg, "ACADEMIC_CONF_CACHE_TTL_DAYS", _DEFAULT_TTL_DAYS))
        if last_updated:
            age = (datetime.now() - datetime.strptime(last_updated, "%Y-%m-%d")).days
            if age < ttl_days:
                return data
    except Exception:
        pass
    return None


def _save_cache(cfg, data: dict) -> None:
    try:
        data["last_updated"] = datetime.now().strftime("%Y-%m-%d")
        p = _cache_path(cfg)
        p.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"    ⚠️  [academic_conf] キャッシュ保存失敗: {str(e)[:60]}")


# ==============================================================
# BLOCK 2: データ取得ユーティリティ
# ==============================================================

def _safe_get(url: str, timeout: int = _REQUEST_TIMEOUT, retries: int = 2) -> Optional[str]:
    try:
        import requests
        for attempt in range(retries):
            try:
                resp = requests.get(url, headers=_REQUEST_HEADERS, timeout=timeout)
                if resp.status_code == 200:
                    return resp.text
                elif resp.status_code in (429, 503):
                    time.sleep(5 * (attempt + 1))
            except Exception:
                time.sleep(2)
        return None
    except ImportError:
        return None


def _detect_affiliations(text: str, known_companies: dict, known_universities: list) -> tuple:
    """
    テキストから企業・大学の所属を検出する。

    Returns:
        (companies: list[dict], universities: list[str])
    """
    companies = []
    universities = []
    text_lower = text.lower()

    for name, info in known_companies.items():
        if name.lower() in text_lower:
            companies.append({
                "name":   name,
                "ticker": info.get("ticker"),
                "code":   info.get("code", name),
            })

    for univ in known_universities:
        if univ.lower() in text_lower:
            universities.append(univ)

    return companies, universities


def _extract_topic_tags(title: str, abstract: str = "") -> list:
    """
    タイトル・アブストラクトから技術トピックタグを抽出する。
    キーワードマッチングによる簡易実装。
    """
    combined = (title + " " + abstract).lower()
    tag_patterns = {
        "llm":           ["llm", "large language", "gpt", "language model"],
        "diffusion":     ["diffusion model", "stable diffusion", "score matching"],
        "transformer":   ["transformer", "attention mechanism", "self-attention"],
        "rl":            ["reinforcement learning", "policy gradient", "reward"],
        "3dnand":        ["3d nand", "nand flash", "v-nand"],
        "dram_hbm":      ["dram", "hbm", "high bandwidth memory"],
        "cmos_logic":    ["cmos", "finfet", "gaa", "gate-all-around"],
        "power_sic":     ["sic", "gan", "wide bandgap", "silicon carbide"],
        "image_sensor":  ["cmos image sensor", "image sensor", "stacked sensor"],
        "photonics":     ["photonics", "silicon photonics", "laser", "optical"],
        "chiplet":       ["chiplet", "die-to-die", "ucle", "advanced packaging"],
        "ev_battery":    ["battery", "solid state", "lithium", "ev"],
        "radar":         ["radar", "lidar", "sensing", "isac"],
        "wireless":      ["5g", "6g", "wifi", "mmwave", "beamforming"],
    }
    tags = []
    for tag, patterns in tag_patterns.items():
        if any(p in combined for p in patterns):
            tags.append(tag)
    return tags


# ==============================================================
# BLOCK 3: arXiv 経由での発表情報取得
# ==============================================================

def _fetch_arxiv_papers(
    conf_config: dict,
    year: int,
    max_results: int = 50,
) -> list:
    """
    arXiv APIから学会関連のプレプリントを取得する。
    学会発表前の事前公開論文から所属企業を検出する。

    Args:
        conf_config: 学会設定辞書
        year:        対象年
        max_results: 最大取得件数

    Returns:
        [{"title", "authors", "affiliations_raw", ...}, ...]
    """
    papers = []
    try:
        import requests

        keywords = conf_config.get("arxiv_keywords", [])
        categories = conf_config.get("arxiv_categories", [])
        if not keywords:
            return papers

        # arXiv APIクエリ構築
        # $query = category\\_filter AND (keyword_1 OR keyword_2 ...)$
        kw_query = " OR ".join(f'ti:"{kw}"' for kw in keywords[:3])
        cat_filter = " OR ".join(f"cat:{c}" for c in categories[:2])
        query = f"({kw_query}) AND ({cat_filter})"

        # 対象年のフィルタ
        date_from = f"{year}01010000"
        date_to   = f"{year}12312359"

        params = {
            "search_query": query,
            "start":        0,
            "max_results":  max_results,
            "sortBy":       "submittedDate",
            "sortOrder":    "descending",
            "start_date": date_from,
            "end_date":   date_to,
        }

        resp = requests.get(
            _ARXIV_API_URL,
            params=params,
            headers=_REQUEST_HEADERS,
            timeout=_REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            return papers

        # XML解析
        import xml.etree.ElementTree as ET
        root = ET.fromstring(resp.text)
        ns = {"atom": "http://www.w3.org/2005/Atom"}

        for entry in root.findall("atom:entry", ns):
            try:
                title = entry.findtext("atom:title", "", ns).strip()
                summary = entry.findtext("atom:summary", "", ns).strip()
                published = entry.findtext("atom:published", "", ns)[:10]

                # 発行年フィルタ
                if not published.startswith(str(year)):
                    continue

                authors = []
                affiliations_raw = []
                for author in entry.findall("atom:author", ns):
                    name = author.findtext("atom:name", "", ns).strip()
                    affil = author.findtext("atom:arxiv:affiliation", "", ns).strip()
                    if name:
                        authors.append(name)
                    if affil:
                        affiliations_raw.append(affil)

                papers.append({
                    "title":            title,
                    "authors":          authors,
                    "affiliations_raw": affiliations_raw,
                    "abstract":         summary[:500],
                    "published":        published,
                    "source":           "arxiv",
                    "topic_tags":       _extract_topic_tags(title, summary),
                })

            except Exception:
                continue

    except Exception:
        pass

    return papers


# ==============================================================
# BLOCK 4: 学会プログラムHTMLの直接解析
# ==============================================================

def _fetch_program_html(conf_config: dict, year: int) -> list:
    """
    学会公式プログラムページのHTMLから発表情報を取得する。

    Returns:
        [{"title", "session", "affiliations_raw", ...}, ...]
    """
    papers = []
    program_urls = conf_config.get("program_urls", {})
    url = program_urls.get(year)
    if not url:
        return papers

    raw = _safe_get(url, timeout=20, retries=2)
    if not raw:
        return papers

    try:
        # BeautifulSoup が利用可能な場合のみHTML解析
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(raw, "html.parser")

        # 汎用的なタイトル・著者パターンの抽出
        # 学会サイトごとに構造が異なるため、複数パターンを試行
        paper_candidates = []

        # パターン1: <h3> or <h4> タグをタイトルとみなす
        for tag in soup.find_all(["h3", "h4"], limit=200):
            text = tag.get_text(strip=True)
            if len(text) > 20:
                # 直後の <p> をアフィリエーションとみなす
                next_p = tag.find_next_sibling("p")
                affil_text = next_p.get_text(strip=True) if next_p else ""
                paper_candidates.append({
                    "title":            text,
                    "affiliations_raw": [affil_text] if affil_text else [],
                    "session":          "",
                    "source":           "html_program",
                    "topic_tags":       _extract_topic_tags(text),
                })

        # パターン2: class名に "paper" / "talk" / "session" を含む要素
        for cls_pattern in ["paper", "talk", "abstract", "session-item"]:
            for elem in soup.find_all(class_=re.compile(cls_pattern, re.I), limit=100):
                title_elem = elem.find(["h3", "h4", "strong", "b"])
                if not title_elem:
                    continue
                title = title_elem.get_text(strip=True)
                if len(title) < 10:
                    continue
                affil_elems = elem.find_all(class_=re.compile("author|affil", re.I))
                affiliations = [e.get_text(strip=True) for e in affil_elems]
                paper_candidates.append({
                    "title":            title,
                    "affiliations_raw": affiliations,
                    "session":          "",
                    "source":           "html_program",
                    "topic_tags":       _extract_topic_tags(title),
                })

        papers = paper_candidates[:100]  # 上限を設けてコスト管理

    except ImportError:
        # BeautifulSoup なし: 生テキストからキーワード検出のみ
        lines = raw.split("\n")
        for line in lines:
            line = line.strip()
            if 20 < len(line) < 200:
                tags = _extract_topic_tags(line)
                if tags:
                    papers.append({
                        "title":            line,
                        "affiliations_raw": [],
                        "session":          "",
                        "source":           "raw_text",
                        "topic_tags":       tags,
                    })
    except Exception:
        pass

    return papers


# ==============================================================
# BLOCK 5: 企業・大学の検出と集計
# ==============================================================

def _process_papers(
    papers: list,
    conf_config: dict,
) -> tuple:
    """
    論文リストから企業・大学の検出と集計を行う。

    Returns:
        (
          processed_papers: list,
          company_paper_counts: dict,  # {ticker: int}
          univ_collab_counts: dict,    # {ticker: univ_collab_count}
        )
    """
    all_companies_jp = conf_config.get("known_contributors_jp", {})
    all_companies_us = conf_config.get("known_contributors_us", {})
    all_companies = {**all_companies_jp, **all_companies_us}
    known_univs = conf_config.get("known_universities", [])

    company_paper_counts: dict = {}
    univ_collab_counts: dict   = {}
    processed = []

    for paper in papers:
        affils_raw = paper.get("affiliations_raw", [])
        combined_affil = " ".join(affils_raw) + " " + paper.get("title", "")

        companies_detected, univs_detected = _detect_affiliations(
            combined_affil, all_companies, known_univs
        )

        # 企業カウント
        for comp in companies_detected:
            ticker = comp.get("ticker")
            if ticker:
                company_paper_counts[ticker] = company_paper_counts.get(ticker, 0) + 1
                if univs_detected:
                    univ_collab_counts[ticker] = univ_collab_counts.get(ticker, 0) + 1

        processed.append({
            **paper,
            "companies_detected":  companies_detected,
            "universities_detected": univs_detected,
        })

    return processed, company_paper_counts, univ_collab_counts


# ==============================================================
# BLOCK 6: スコア計算
# ==============================================================

def _calc_academic_signal_score(
    company_paper_counts: dict,
    total_papers: int,
) -> float:
    """
    カンファレンス単位のシグナルスコアを計算する。
    $signal\\_score = \\frac{top\\_company\\_papers}{total\\_papers} \\times 10$
    Returns: float 0.0 ~ 10.0
    """
    if not company_paper_counts or total_papers == 0:
        return 0.0
    max_papers = max(company_paper_counts.values())
    return round(min(10.0, (max_papers / max(1, total_papers)) * 10.0), 2)


def _calc_company_academic_scores(conferences: dict) -> dict:
    """
    企業ごとの総合学術スコア（0〜100）を計算する。

    $academic\\_score = \\min(100, \\sum_{c} paper\\_score_c \\times field\\_weight_c)$
    field_weight: ai_ml=1.5 / semiconductor=1.2 / physics=1.0
    """
    field_weights = {"ai_ml": 1.5, "semiconductor": 1.2, "physics": 1.0}

    ticker_data: dict = {}

    for conf_id, conf_data in conferences.items():
        field = conf_data.get("field", "other")
        w     = field_weights.get(field, 1.0)
        company_counts = conf_data.get("company_paper_counts", {})
        total_papers   = max(1, conf_data.get("total_papers", 1))
        univ_collabs   = conf_data.get("univ_collab_counts", {})

        for ticker, count in company_counts.items():
            if ticker not in ticker_data:
                ticker_data[ticker] = {
                    "ticker":               ticker,
                    "academic_score":       0.0,
                    "total_papers":         0,
                    "leading_confs":        [],
                    "univ_collab_count":    0,
                    "total_papers_for_collab": 0,
                    "fields":              set(),
                }
            entry = ticker_data[ticker]
            normalized = count / total_papers
            entry["academic_score"]    += normalized * w * 100.0
            entry["total_papers"]      += count
            entry["univ_collab_count"] += univ_collabs.get(ticker, 0)
            entry["total_papers_for_collab"] += count
            entry["fields"].add(field)
            if count >= 3:
                entry["leading_confs"].append(conf_id)

    result = {}
    for ticker, entry in ticker_data.items():
        total_for_collab = max(1, entry["total_papers_for_collab"])
        entry["university_collab_ratio"] = round(
            entry["univ_collab_count"] / total_for_collab, 3
        )
        entry["academic_score"] = round(
            min(_ACADEMIC_SCORE_MAX, entry["academic_score"]), 1
        )
        entry["fields"] = sorted(entry["fields"])
        del entry["univ_collab_count"]
        del entry["total_papers_for_collab"]
        result[ticker] = entry

    return result


# ==============================================================
# BLOCK 7: 外部参照用アクセサ
# ==============================================================

def get_academic_score(ticker: str, cfg=None) -> Optional[float]:
    """
    銘柄の学術スコアを academic_conf_cache.json から取得する。
    Returns: float 0.0〜100.0 または None
    """
    try:
        cache = _load_cache(cfg)
        if not cache:
            return None
        return cache.get("company_scores", {}).get(ticker, {}).get("academic_score")
    except Exception:
        return None


def get_leading_conferences(ticker: str, cfg=None) -> list:
    """
    銘柄がリードしているカンファレンスIDリストを返す。
    Returns: ["isscc_2025", ...] 形式のリスト
    """
    try:
        cache = _load_cache(cfg)
        if not cache:
            return []
        return cache.get("company_scores", {}).get(ticker, {}).get("leading_confs", [])
    except Exception:
        return []


def get_conference_leaders(conf_id: str, cfg=None, top_n: int = 10) -> list:
    """
    指定カンファレンスのトップN企業を返す。
    Returns: [{"ticker": str, "paper_count": int}, ...]
    """
    try:
        cache = _load_cache(cfg)
        if not cache:
            return []
        conf = cache.get("conferences", {}).get(conf_id, {})
        counts = conf.get("company_paper_counts", {})
        sorted_list = sorted(counts.items(), key=lambda x: x[1], reverse=True)
        return [{"ticker": t, "paper_count": c} for t, c in sorted_list[:top_n]]
    except Exception:
        return []


# ==============================================================
# BLOCK 8: メイン実行フロー
# ==============================================================

def run_academic_conf_tracker(cfg=None, force_refresh: bool = False) -> dict:
    """
    stock_analyzer_main.py の WEEKLY_MODULES から呼ばれるエントリポイント。

    処理フロー:
      1. キャッシュ有効 → 返却
      2. 各学会のデータ取得（arXiv優先 → HTML解析）
      3. 企業・大学の検出
      4. スコア計算
      5. academic_conf_cache.json に保存

    Returns:
        {
          "conferences":     dict,
          "company_scores":  dict,
          "stats":           dict,
          "cached":          bool,
          "timestamp":       str,
        }
    """
    print("\n  🎓 学術カンファレンス追跡（Wave 7 - Step 2）実行中...")

    # ── キャッシュ確認 ──────────────────────────────────────────
    if not force_refresh:
        cached = _load_cache(cfg)
        if cached:
            stats = cached.get("stats", {})
            print(
                f"    ✅ キャッシュ使用: "
                f"学会={stats.get('total_confs', 0)} / "
                f"論文={stats.get('total_papers', 0)} / "
                f"企業={stats.get('total_companies', 0)}"
            )
            return {
                "conferences":    cached.get("conferences", {}),
                "company_scores": cached.get("company_scores", {}),
                "stats":          stats,
                "cached":         True,
                "timestamp":      datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }

    conf_config = getattr(cfg, "ACADEMIC_CONF_CONFIG", _DEFAULT_CONF_CONFIG)
    all_conferences: dict = {}
    total_papers_global = 0

    for conf_key, conf_def in conf_config.items():
        years = conf_def.get("years", [datetime.now().year])
        for year in years:
            conf_id = f"{conf_key}_{year}"
            print(f"    📋 {conf_def.get('name')} {year} データ取得中...")
            try:
                # Level 1: arXiv経由
                papers = _fetch_arxiv_papers(conf_def, year, max_results=50)
                print(f"       arXiv: {len(papers)} 論文取得")

                # Level 2: 不足分はHTML補完（arXivが10件未満の場合）
                if len(papers) < 10:
                    html_papers = _fetch_program_html(conf_def, year)
                    papers.extend(html_papers)
                    print(f"       HTML補完: +{len(html_papers)} 論文")

                # 企業・大学の検出
                processed, company_counts, univ_collabs = _process_papers(
                    papers, conf_def
                )

                total_papers_global += len(processed)
                sig_score = _calc_academic_signal_score(company_counts, len(processed))

                all_conferences[conf_id] = {
                    "conf_id":             conf_id,
                    "name":                conf_def.get("name", conf_key),
                    "full_name":           conf_def.get("full_name", ""),
                    "year":                year,
                    "field":               conf_def.get("field", "other"),
                    "tech_node_ids":       conf_def.get("tech_node_ids", []),
                    "papers":              processed[:50],  # 上位50件のみ格納
                    "company_paper_counts": company_counts,
                    "univ_collab_counts":  univ_collabs,
                    "total_papers":        len(processed),
                    "signal_score":        sig_score,
                }

                if company_counts:
                    top_ticker = max(company_counts, key=company_counts.get)
                    print(
                        f"       完了: {len(processed)}論文 / "
                        f"最多企業={top_ticker}({company_counts[top_ticker]}本)"
                    )

            except Exception as e:
                print(f"    ⚠️  [{conf_id}] エラー: {str(e)[:60]}")
                all_conferences[conf_id] = {
                    "conf_id":             conf_id,
                    "name":                conf_def.get("name", conf_key),
                    "year":                year,
                    "field":               conf_def.get("field", "other"),
                    "tech_node_ids":       conf_def.get("tech_node_ids", []),
                    "papers":              [],
                    "company_paper_counts": {},
                    "univ_collab_counts":  {},
                    "total_papers":        0,
                    "signal_score":        0.0,
                }

    # ── 企業別スコア集計 ──────────────────────────────────────
    company_scores = _calc_company_academic_scores(all_conferences)

    # ── 統計 ─────────────────────────────────────────────────
    top_companies = sorted(
        [(t, v["academic_score"]) for t, v in company_scores.items()],
        key=lambda x: x[1],
        reverse=True,
    )[:5]

    stats = {
        "total_confs":     len(all_conferences),
        "total_papers":    total_papers_global,
        "total_companies": len(company_scores),
        "top_5_companies": [{"ticker": t, "score": s} for t, s in top_companies],
        "fields_tracked":  list(set(v.get("field", "") for v in all_conferences.values())),
    }

    # ── 保存 ─────────────────────────────────────────────────
    cache_data = {
        "conferences":    all_conferences,
        "company_scores": company_scores,
        "stats":          stats,
        "version":        "1.0",
    }
    _save_cache(cfg, cache_data)

    print(
        f"    ✅ 学術追跡完了: "
        f"学会={len(all_conferences)} / 論文={total_papers_global} / "
        f"企業={len(company_scores)}"
    )
    if top_companies:
        top_str = " / ".join(f"{t}:{s:.1f}" for t, s in top_companies[:3])
        print(f"    🏆 学術スコア上位3: {top_str}")

    return {
        "conferences":    all_conferences,
        "company_scores": company_scores,
        "stats":          stats,
        "cached":         False,
        "timestamp":      datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ==============================================================
# BLOCK 9: 単体テスト
# ==============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("  🎓 academic_conf_tracker.py v1.0 単体テスト")
    print("=" * 60)

    result = run_academic_conf_tracker(cfg=None, force_refresh=True)

    print("\n  【テスト結果】")
    print(f"  追跡学会数  : {result['stats'].get('total_confs', 0)}")
    print(f"  総論文数    : {result['stats'].get('total_papers', 0)}")
    print(f"  追跡企業数  : {result['stats'].get('total_companies', 0)}")

    confs = result.get("conferences", {})
    if confs:
        sample_id = next(iter(confs))
        sample = confs[sample_id]
        print(f"\n  【サンプル学会: {sample_id}】")
        print(f"  名称        : {sample.get('name')}")
        print(f"  分野        : {sample.get('field')}")
        print(f"  論文数      : {sample.get('total_papers')}")
        print(f"  シグナルスコア: {sample.get('signal_score')}")
        counts = sample.get("company_paper_counts", {})
        for ticker, cnt in sorted(counts.items(), key=lambda x: x[1], reverse=True)[:3]:
            print(f"    {ticker:12s}: {cnt} 本")

    company_scores = result.get("company_scores", {})
    if company_scores:
        print("\n  【企業学術スコア（上位5）】")
        sorted_cs = sorted(
            company_scores.items(),
            key=lambda x: x[1].get("academic_score", 0),
            reverse=True,
        )[:5]
        for ticker, cdata in sorted_cs:
            print(
                f"    {ticker:12s}: "
                f"score={cdata.get('academic_score', 0):6.1f} "
                f"papers={cdata.get('total_papers', 0):3d} "
                f"collab={cdata.get('university_collab_ratio', 0):.2f}"
            )

    print("\n  ✅ 単体テスト完了")