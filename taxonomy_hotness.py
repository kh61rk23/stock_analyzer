# ==============================================================
# taxonomy_hotness.py — T-4: node_hotness 算出（Wave R2-5）
# ==============================================================
# 【役割】taxonomyノードごとの「テーマ加熱度」を日次算出する。
# 【設計正本】HANDOFF_2026-06-30 §4-3（決定3の具体化・変更禁止）:
#   - 準先行（semi-leading・主スコア駆動）: news_pipeline のテーマ別
#     ニュース量（最重み）＋ policy_radar の政策言及
#   - 先行（leading・早期検出）: arxiv の論文増加
#   - 両者を**別々に保持**して最終合成。「先行だけ点灯＝まだニュース化して
#     いない＝仕込み余地」を早期に拾えるよう leading/semi を個別に出力する。
# 【対象ノード】data/taxonomy_seed/ の verified_flag=true ノードのみ
#   （承認が増えると自動で対象が広がる）。
# 【出力】
#   - stock_data/cache/taxonomy/node_hotness.json（履歴60日保持）
#   - results["taxonomy_signal"] = {"node_hotness": {...},
#       "sector_scores": {sector_code: 0-100}, "leading_only": [...]}
#     sector_scores は size_tier_classifier の業種→ノード橋渡しを逆引きし、
#     signal_aggregator カテゴリV（T-5）が消費する。
# ==============================================================

from __future__ import annotations

import json
import re
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

SEED_DIR = REPO / "data" / "taxonomy_seed"
HISTORY_DAYS = 60          # 履歴保持日数
BASELINE_DAYS = 20         # 加熱判定のベースライン日数

from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'TH_SEMI_NEWS_WEIGHT': 'SEMI_NEWS_WEIGHT',
    'TH_SEMI_POLICY_WEIGHT': 'SEMI_POLICY_WEIGHT',
    'B4_TAXONOMY_HOTNESS_HISTORY_DAYS': 'HISTORY_DAYS',
    'B4_TAXONOMY_HOTNESS_BASELINE_DAYS': 'BASELINE_DAYS',
    'B6_TAXONOMY_HOTNESS_COMBINE_SEMI': 'COMBINE_SEMI',
    'B6_TAXONOMY_HOTNESS_COMBINE_LEADING': 'COMBINE_LEADING',
}

SEMI_NEWS_WEIGHT = 0.7     # 準先行内: news（最重み・§4-3）
SEMI_POLICY_WEIGHT = 0.3   # 準先行内: policy
COMBINE_SEMI = 0.6         # 最終合成: 準先行（主スコア駆動）
COMBINE_LEADING = 0.4      # 最終合成: 先行


# ── ノード・キーワード構築（pytest対象） ──────────────────────

def load_verified_nodes(seed_dir: Path = SEED_DIR) -> list[dict]:
    """seedファイルから verified_flag=true のノードのみ読み込む。"""
    nodes = []
    for p in sorted(seed_dir.glob("batch*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            nodes.extend(n for n in d.get("nodes", []) if n.get("verified_flag"))
        except Exception:
            continue
    return nodes


_STOPWORDS = {"ic", "ai", "the", "and", "of", "for", "framework", "platform",
              "software", "device", "system", "component", "material"}


def build_node_keywords(nodes: list[dict]) -> dict:
    """
    node_id → 照合キーワード集合。
    英名（2文字以下・一般語は除外）＋日本語名＋node_idのトークン。
    """
    out = {}
    for n in nodes:
        kws = set()
        en = (n.get("node_name_en") or "").lower()
        if en:
            kws.add(en)                              # フレーズ全体
            for tok in re.split(r"[^a-z0-9]+", en):
                if len(tok) >= 3 and tok not in _STOPWORDS:
                    kws.add(tok)
        ja = n.get("node_name_ja") or ""
        # 括弧注記を除いた日本語名（照合は小文字化テキスト相手のためlower）
        ja_clean = re.sub(r"[（(].*?[)）]", "", ja).strip().lower()
        if len(ja_clean) >= 2:
            kws.add(ja_clean)
        for tok in n.get("node_id", "").split("_"):
            # HBM/GaN/SiC等の頭字語を拾うため3文字以上（一般語はstopwordsで除外）
            if len(tok) >= 3 and tok not in _STOPWORDS:
                kws.add(tok)
        out[n["node_id"]] = kws
    return out


def match_counts(texts: list[str], node_keywords: dict) -> dict:
    """テキスト群に対する node_id 別ヒット記事数（1テキスト1カウント）。"""
    counts = {nid: 0 for nid in node_keywords}
    for text in texts:
        t = (text or "").lower()
        if not t:
            continue
        for nid, kws in node_keywords.items():
            if any(kw in t for kw in kws):
                counts[nid] += 1
    return counts


def hotness_score(today_count: int, baseline_counts: list[int]) -> float:
    """
    当日ヒット数をベースライン（過去N日平均）比で 0-100 化。
    50=平常（ベースライン並み）・100=ベースラインの3倍以上・0=ヒットなし継続。
    履歴不足時は当日ヒット有無で 50/60 を返す（初期は中立寄り）。
    """
    if not baseline_counts:
        return 60.0 if today_count > 0 else 50.0
    base = sum(baseline_counts) / len(baseline_counts)
    if base <= 0:
        return min(100.0, 50.0 + today_count * 10.0) if today_count else 50.0
    ratio = today_count / base
    return round(max(0.0, min(100.0, 50.0 + (ratio - 1.0) * 25.0)), 1)


def compute_node_hotness(today_counts: dict, history: dict,
                         today: str, cfg=None) -> dict:
    """
    各ノードの leading/semi_leading/combined を計算する。
    today_counts: {node_id: {"leading": int, "news": int, "policy": int}}
    history:      {date: {node_id: {"leading": int, "news": int, "policy": int}}}
    """
    # 2026-07-25 バッチ6: ベースライン日数を config から読む
    # （従来はモジュール定数 BASELINE_DAYS のみで config 側は未参照）
    _base_days = int(getattr(cfg, "TAXONOMY_HOTNESS_BASELINE_DAYS", BASELINE_DAYS))
    past_dates = sorted(d for d in history if d < today)[-_base_days:]
    out = {}
    for nid, c in today_counts.items():
        base_lead = [history[d].get(nid, {}).get("leading", 0) for d in past_dates]
        base_news = [history[d].get(nid, {}).get("news", 0) for d in past_dates]
        base_pol = [history[d].get(nid, {}).get("policy", 0) for d in past_dates]
        leading = hotness_score(c.get("leading", 0), base_lead)
        semi = round(SEMI_NEWS_WEIGHT * hotness_score(c.get("news", 0), base_news)
                     + SEMI_POLICY_WEIGHT * hotness_score(c.get("policy", 0), base_pol), 1)
        combined = round(COMBINE_SEMI * semi + COMBINE_LEADING * leading, 1)
        out[nid] = {"leading": leading, "semi_leading": semi,
                    "combined": combined,
                    "raw": {"leading": c.get("leading", 0),
                            "news": c.get("news", 0),
                            "policy": c.get("policy", 0)}}
    return out


def leading_only_nodes(hotness: dict, lead_th: float = None,
                       semi_th: float = 55.0, cfg=None) -> list:
    """「先行だけ点灯」ノード＝まだニュース化していない仕込み候補（§4-3）。

    lead_th 省略時は config.TAXONOMY_LEADING_ONLY_TH（既定65.0）を使う
    （2026-07-25 バッチ6: 従来は既定引数の直書きのみだった）。
    """
    if lead_th is None:
        lead_th = float(getattr(cfg, "TAXONOMY_LEADING_ONLY_TH", 65.0))
    return sorted(
        (nid for nid, h in hotness.items()
         if h["leading"] >= lead_th and h["semi_leading"] < semi_th),
        key=lambda nid: -hotness[nid]["leading"])


def sector_scores_from_hotness(hotness: dict, sector_to_nodes: dict) -> dict:
    """
    業種→ノード橋渡しの逆引きで業種別スコア（0-100・データなしはキーなし）。
    sector_to_nodes は2形式を受ける:
      - {code: [node_id, ...]}            → 均等平均（フォールバック）
      - {code: {node_id: relevance, ...}} → relevance加重平均（T-3消費経路）
    """
    out = {}
    for code, spec in (sector_to_nodes or {}).items():
        if isinstance(spec, dict):
            num = sum(hotness[nid]["combined"] * w
                      for nid, w in spec.items() if nid in hotness)
            den = sum(w for nid, w in spec.items() if nid in hotness)
            if den > 0:
                out[str(code)] = round(num / den, 1)
        else:
            vals = [hotness[nid]["combined"] for nid in spec if nid in hotness]
            if vals:
                out[str(code)] = round(sum(vals) / len(vals), 1)
    return out


def fetch_sector_node_weights(cfg, sector_codes: list) -> dict:
    """
    [T-3消費配線 / Wave R2] company_taxonomy_map の関与度を業種ごとに集約して返す。
    {sector_code: {node_id: relevance_score}}。BQ未接続・空なら {} を返し、
    呼び出し側は _SECTOR_TO_TAXONOMY にフォールバックする。

    2026-09-21: 業種コードで company_taxonomy_map を引いていたが、表のキーは
    4桁の企業コードで業種擬似ID（"sector:JP:1016"）の行は1件も無く、毎日
    フォールバック（均等加重）に落ちていた。さらに業種ごとに1本ずつBQを
    引く N+1 でもあった。業種→銘柄（1本）→銘柄の関与度（1本）の計2本で集約する。
    """
    try:
        from edinet_bq_client import EDINETBQClient
        bq = EDINETBQClient()
    except Exception:
        return {}

    wanted = {str(c) for c in (sector_codes or [])}
    try:
        tickers_by_sector = bq.get_tickers_by_sector_bulk(
            limit=int(getattr(cfg, "TAXONOMY_SECTOR_TICKER_LIMIT", 10))
        ) or {}
    except Exception:
        return {}

    codes_by_sector = {
        sec: [str(t).split(".")[0] for t in tickers if t]
        for sec, tickers in tickers_by_sector.items()
        if not wanted or str(sec) in wanted
    }
    all_codes = {c for codes in codes_by_sector.values() for c in codes}
    if not all_codes:
        return {}

    try:
        scores_by_code = bq.get_taxonomy_scores_bulk(sorted(all_codes))
    except Exception:
        return {}
    if not scores_by_code:
        return {}

    out: dict = {}
    for sec, codes in codes_by_sector.items():
        totals: dict = {}
        for code in codes:
            for node_id, score in (scores_by_code.get(code) or {}).items():
                totals.setdefault(node_id, []).append(float(score))
        if totals:
            out[str(sec)] = {
                node_id: round(sum(vals) / len(vals), 4)
                for node_id, vals in totals.items()
            }
    return out


# ── 収集・I/O ────────────────────────────────────────────────

def collect_texts(results: dict) -> dict:
    """results から照合対象テキストを収集。{"leading": [...], "news": [...], "policy": [...]}"""
    leading, news, policy = [], [], []
    for p in ((results.get("arxiv") or {}).get("papers") or []):
        leading.append(f"{p.get('title', '')} {p.get('summary', '')}")
    for a in ((results.get("news") or {}).get("articles") or []):
        news.append(f"{a.get('title', '')} {a.get('summary', '')}")
    for it in ((results.get("policy_radar") or {}).get("items") or []):
        policy.append(f"{it.get('title', '')} {it.get('summary', '')}")
    return {"leading": leading, "news": news, "policy": policy}


def _cache_path(cfg) -> Path:
    base = Path(getattr(cfg, "CACHE_DIR", "stock_data/cache"))
    p = base / "taxonomy"
    p.mkdir(parents=True, exist_ok=True)
    return p / "node_hotness.json"


def run_taxonomy_hotness(cfg, results: dict) -> dict:
    """
    メインエントリ（stock_analyzer_main の日次ステージ）。
    Returns: {"node_hotness": {...}, "sector_scores": {...},
              "leading_only": [...], "nodes": int, "skipped": bool}
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    nodes = load_verified_nodes()
    if not nodes:
        print("    ⏩ [taxonomy_hotness] 承認済みノードなし → スキップ")
        return {"node_hotness": {}, "sector_scores": {}, "leading_only": [],
                "nodes": 0, "skipped": True}
    keywords = build_node_keywords(nodes)
    texts = collect_texts(results)
    today = date.today().isoformat()

    today_counts = {}
    lead_c = match_counts(texts["leading"], keywords)
    news_c = match_counts(texts["news"], keywords)
    pol_c = match_counts(texts["policy"], keywords)
    for nid in keywords:
        today_counts[nid] = {"leading": lead_c[nid], "news": news_c[nid],
                             "policy": pol_c[nid]}

    # 履歴読込→当日追記→保持期間クランプ
    path = _cache_path(cfg)
    history = {}
    if path.exists():
        try:
            history = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            history = {}
    hotness = compute_node_hotness(today_counts, history, today, cfg)
    history[today] = today_counts
    for old in sorted(history)[:-HISTORY_DAYS]:
        del history[old]
    try:
        path.write_text(json.dumps(history, ensure_ascii=False),
                        encoding="utf-8")
    except Exception as e:
        print(f"    ⚠️ [taxonomy_hotness] 履歴保存失敗: {str(e)[:60]}")

    # 業種スコア: まず company_taxonomy_map の関与度加重（T-3消費）を試み、
    # BQ空/失敗時は _SECTOR_TO_TAXONOMY の均等加重にフォールバック。
    sector_scores = {}
    weight_source = "fallback(_SECTOR_TO_TAXONOMY)"
    try:
        from size_tier_classifier import _SECTOR_TO_TAXONOMY
        codes = list(_SECTOR_TO_TAXONOMY.keys())
        bq_weights = fetch_sector_node_weights(cfg, codes)
        if bq_weights:
            sector_scores = sector_scores_from_hotness(hotness, bq_weights)
            weight_source = f"company_taxonomy_map({len(bq_weights)}業種)"
        else:
            sector_scores = sector_scores_from_hotness(hotness, _SECTOR_TO_TAXONOMY)
    except Exception:
        sector_scores = {}

    lead_only = leading_only_nodes(hotness, cfg=cfg)
    hot_top = sorted(hotness.items(), key=lambda kv: -kv[1]["combined"])[:3]
    print(f"    ✅ [taxonomy_hotness] {len(nodes)}ノード算出 "
          f"/ 業種スコア{len(sector_scores)}件({weight_source}) / 先行のみ点灯{len(lead_only)}件"
          + (f" / top: {hot_top[0][0]}({hot_top[0][1]['combined']})" if hot_top else ""))
    return {"node_hotness": hotness, "sector_scores": sector_scores,
            "leading_only": lead_only, "weight_source": weight_source,
            "nodes": len(nodes), "skipped": False}
