# ==============================================================
# finbert_analyzer.py  v1.0  (RESEARCH-5)
# FinBERT金融感情分析パイプライン
# ==============================================================
# 【役割】
#   news_pipeline.py のキーワードマッチング（0/1判定）を
#   FinBERT（金融特化BERT）の文脈理解感情分析に置き換える。
#   「業績が予想を大幅に上回った」→ POSITIVE（0.92）を正確に識別。
#
# 【使用モデル優先順】
#   1. ProsusAI/finbert（英語版・金融特化・Hugging Face無料）
#   2. yiyanghkust/finbert-tone（英語版・感情3クラス）
#   3. sonoisa/sentence-bert-base-ja-mean-tokens（日本語版・Sentenceレベル）
#   ※ 日本語記事は英語モデルに翻訳せず、タイトルの主要語を英語で推論
#
# 【処理設計（GitHub Actions制約対応）】
#   - 初回実行: Hugging Face からモデルをDL → stock_data/models/ にキャッシュ
#   - 2回目以降: ローカルキャッシュを読み込み（DL不要・高速）
#   - .gitignore に stock_data/models/ を追加（モデルをgit管理しない）
#   - CPU推論: 記事100本 ≈ 30秒（WEEKLY_MODULES での実行を推奨）
#   - 感情キャッシュ: 7日有効（trends_cacheと同じ設計）
#
# 【出力スキーマ（sentiment_cache.json）】
#   {
#     "cached_at": "2026-03-29",
#     "by_sector": {
#       "1016": {
#         "avg_sentiment":   0.72,   # 0〜1（1=最高positive）
#         "positive_count":  8,
#         "negative_count":  2,
#         "neutral_count":   5,
#         "sentiment_trend": "improving",  # "improving"/"deteriorating"/"stable"
#         "top_positive":    [Article, ...],
#         "top_negative":    [Article, ...],
#       }
#     },
#     "articles_scored": int,
#     "model_used":      str,
#   }
#
# 【依存】transformers / torch（オプション）/ config.py / news_pipeline.py
# 【呼出】from finbert_analyzer import run_finbert_analyzer,
#                                       get_sector_sentiment
# ==============================================================

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
# analyze_sections 用シングルトン pipeline キャッシュ
_FINBERT_PIPELINE = None

# ==============================================================
# BLOCK 0: 定数
# ==============================================================

SENTIMENT_CACHE_FILE = "sentiment_cache.json"
from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'B4_FINBERT_ANALYZER_CACHE_VALID_DAYS': 'CACHE_VALID_DAYS',
    'B5_FINBERT_ANALYZER_DEFAULT_BATCH_SIZE': 'DEFAULT_BATCH_SIZE',
    'B6_FINBERT_ANALYZER_MAX_TEXT_LEN': 'MAX_TEXT_LEN',
    'B6_FINBERT_ANALYZER_SENTIMENT_STRONG_POS': 'SENTIMENT_STRONG_POS',
    'B6_FINBERT_ANALYZER_SENTIMENT_STRONG_NEG': 'SENTIMENT_STRONG_NEG',
    'B6_FINBERT_ANALYZER_SENTIMENT_TAILWIND_PTS': 'SENTIMENT_TAILWIND_PTS',
}

CACHE_VALID_DAYS     = 7
MODEL_CACHE_DIR      = "models"      # stock_data/models/ に保存

# モデル優先順位（上から試す）
FINBERT_MODELS = [
    "ProsusAI/finbert",          # 最も金融特化・精度高
    "yiyanghkust/finbert-tone",  # 感情3クラス（Positive/Negative/Neutral）
]

# ラベル正規化マッピング（モデルによってラベルが異なる）
LABEL_NORMALIZE = {
    # ProsusAI/finbert
    "positive": "POSITIVE",
    "negative": "NEGATIVE",
    "neutral":  "NEUTRAL",
    # yiyanghkust/finbert-tone
    "Positive": "POSITIVE",
    "Negative": "NEGATIVE",
    "Neutral":  "NEUTRAL",
    # 汎用
    "LABEL_0": "NEGATIVE",
    "LABEL_1": "NEUTRAL",
    "LABEL_2": "POSITIVE",
}

# バッチサイズ（CPUメモリ制約対策）
DEFAULT_BATCH_SIZE = 8
MAX_TEXT_LEN       = 128   # トークン上限（512のモデルでも128に制限して高速化）

# 感情スコアの閾値
SENTIMENT_STRONG_POS = 0.70   # これ以上: 強いポジティブ
SENTIMENT_STRONG_NEG = 0.30   # これ未満: 強いネガティブ（1-score）

# LEADING_INDICATOR への加算点
SENTIMENT_TAILWIND_PTS = 3.0
SENTIMENT_HEADWIND_PTS = -3.0


# ==============================================================
# BLOCK 1: モデルロード（初回のみDL・以降はキャッシュ）
# ==============================================================

def _model_cache_dir(cfg) -> Path:
    """モデルキャッシュディレクトリを返す。"""
    base = Path(getattr(cfg, "DATA_DIR", "stock_data/data")).parent
    return base / MODEL_CACHE_DIR


def _try_import_transformers():
    """transformers をインポートする。失敗時は None。"""
    try:
        from transformers import pipeline as hf_pipeline
        return hf_pipeline
    except ImportError:
        return None


def load_finbert_pipeline(cfg):
    """
    FinBERT パイプラインをロードする。
    優先順位: ローカルキャッシュ → Hugging Face DL

    Returns:
        (pipeline, model_name: str) | (None, None)
    """
    hf_pipeline = _try_import_transformers()
    if hf_pipeline is None:
        print("    ⚠️  transformers 未インストール → スキップ")
        print("    → requirements.txt に transformers>=4.30.0 を追加してください")
        return None, None

    model_name = getattr(cfg, "FINBERT_MODEL_NAME", FINBERT_MODELS[0])
    cache_dir  = str(_model_cache_dir(cfg))
    batch_size = getattr(cfg, "FINBERT_BATCH_SIZE", DEFAULT_BATCH_SIZE)

    # モデルリストを設定モデルで先頭にする
    models_to_try = [model_name] + [m for m in FINBERT_MODELS if m != model_name]

    for model in models_to_try:
        try:
            print(f"    📥 FinBERT ロード中: {model}...")
            nlp = hf_pipeline(
                "sentiment-analysis",
                model      = model,
                cache_dir  = cache_dir,
                truncation = True,
                max_length = MAX_TEXT_LEN,
                batch_size = batch_size,
            )
            print(f"    ✅ FinBERT ロード完了: {model}")
            return nlp, model
        except Exception as e:
            print(f"    ⚠️  {model} ロード失敗: {str(e)[:60]}")
            continue

    return None, None


# ==============================================================
# BLOCK 2: バッチ推論
# ==============================================================

def _normalize_label(label: str) -> str:
    """モデル依存のラベル文字列を POSITIVE/NEGATIVE/NEUTRAL に正規化する。"""
    return LABEL_NORMALIZE.get(label, label.upper())


def run_batch_inference(
    nlp,
    texts: list[str],
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> list[dict]:
    """
    テキストリストに対してバッチ推論を実行する。

    Args:
        nlp:        load_finbert_pipeline() で取得したパイプライン
        texts:      推論対象テキストのリスト（タイトル or 本文先頭）
        batch_size: バッチサイズ

    Returns:
        [{label: str, score: float, sentiment_score: float}, ...]
        sentiment_score: POSITIVE=score, NEGATIVE=1-score, NEUTRAL=0.5
    """
    if not texts or nlp is None:
        return []

    results = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        try:
            preds = nlp(batch, truncation=True, max_length=MAX_TEXT_LEN)
            for pred in preds:
                label     = _normalize_label(pred.get("label", "NEUTRAL"))
                raw_score = float(pred.get("score", 0.5))

                # sentiment_score: 0〜1（0=最ネガティブ / 1=最ポジティブ）
                if label == "POSITIVE":
                    sent_score = raw_score
                elif label == "NEGATIVE":
                    sent_score = 1.0 - raw_score
                else:
                    sent_score = 0.5

                results.append({
                    "label":           label,
                    "score":           round(raw_score, 4),
                    "sentiment_score": round(sent_score, 4),
                })
        except Exception as e:
            # バッチが失敗した場合は中立で埋める
            for _ in batch:
                results.append({
                    "label": "NEUTRAL", "score": 0.5, "sentiment_score": 0.5
                })
            print(f"    ⚠️  バッチ推論失敗（中立代替）: {str(e)[:60]}")

    return results


def enrich_articles_with_sentiment(
    articles: list[dict],
    nlp,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> list[dict]:
    """
    news_pipeline の articles リストに sentiment_score / sentiment_label を追加する。

    Args:
        articles:   news_pipeline.run_news_pipeline() の "articles" リスト
        nlp:        FinBERT パイプライン
        batch_size: バッチサイズ

    Returns:
        sentiment フィールドを追加した articles リスト
    """
    if not articles or nlp is None:
        return articles

    # タイトルをバッチ入力として準備
    texts = []
    for a in articles:
        title   = a.get("title", "")
        summary = a.get("summary", "")
        # タイトルと要約の先頭100文字を結合して推論精度を上げる
        text = f"{title}. {summary[:100]}" if summary else title
        texts.append(text[:MAX_TEXT_LEN * 4])   # トークナイザーの入力上限を考慮

    sentiments = run_batch_inference(nlp, texts, batch_size)

    for article, sent in zip(articles, sentiments):
        article["sentiment_label"] = sent["label"]
        article["sentiment_score"] = sent["sentiment_score"]
        # importance_score を sentiment で補正（最大 +3pt）
        base_importance = article.get("importance_score", 0)
        if sent["label"] == "POSITIVE" and sent["score"] >= 0.7:
            article["importance_score"] = base_importance + 2
        elif sent["label"] == "NEGATIVE" and sent["score"] >= 0.7:
            article["importance_score"] = base_importance + 1  # 悪いニュースも重要

    return articles

# ==============================================================
# BLOCK 2-B: セクション別テキスト分析（Wave 5 - E4対応）
# ==============================================================

def analyze_sections(
    sections_dict: dict,
    nlp=None,
    cfg=None,
) -> dict:
    """
    text_extractor.extract_sections() の出力（セクション別テキストdict）を受け取り、
    各セクションの感情スコアを返す。

    Args:
        sections_dict: {
            "securities_code": str,
            "company_name":    str,
            "period_end":      str,
            "sections": {
                "business":   str,
                "risk":       str,
                "strategy":   str,
                "governance": str,
            },
            "source":     str,
            "fetched_at": str,
        }
        nlp: transformers pipeline（省略時は内部で初期化を試みる）
        cfg: config モジュール（省略可）

    Returns:
        {
            "securities_code": str,
            "period_end":      str,
            "section_scores": {
                "business":   {"label": str, "score": float, "sentiment_score": float},
                "risk":       {...},
                "strategy":   {...},
                "governance": {...},
            },
            "analyzed_at": str (ISO8601),
        }
        分析失敗セクションは score=0.5, label="NEUTRAL" で補完。
        全セクション失敗時は None を返す。
    """
    if not sections_dict or not isinstance(sections_dict, dict):
        return None

    inner = sections_dict.get("sections", {})
    if not inner or not any(inner.values()):
        return None

    # nlp pipeline の初期化（未注入時）
    _nlp = nlp
    if _nlp is None:
        try:
            _nlp = _get_or_init_pipeline()
        except Exception as _e:
            print(f"    ⚠️  [finbert] analyze_sections: pipeline初期化失敗 → {str(_e)[:60]}")
            return None

    section_scores: dict = {}
    _SECTIONS = ("business", "risk", "strategy", "governance")

    for sec_key in _SECTIONS:
        text = inner.get(sec_key, "") or ""
        if not text.strip():
            # テキスト未取得セクションはニュートラルで補完
            section_scores[sec_key] = {
                "label":           "NEUTRAL",
                "score":           0.5,
                "sentiment_score": 0.5,
            }
            continue

        try:
            # FinBERT の最大トークン長（512）に収まるよう先頭512文字を使用
            truncated = text[:512]
            result    = _nlp(truncated)[0]

            raw_label = result.get("label", "NEUTRAL").upper()
            raw_score = float(result.get("score", 0.5))

            # sentiment_score: POSITIVE=0.5〜1.0, NEUTRAL=0.5, NEGATIVE=0.0〜0.5
            if raw_label == "POSITIVE":
                sentiment_score = 0.5 + raw_score * 0.5
            elif raw_label == "NEGATIVE":
                sentiment_score = 0.5 - raw_score * 0.5
            else:
                sentiment_score = 0.5

            section_scores[sec_key] = {
                "label":           raw_label,
                "score":           round(raw_score, 4),
                "sentiment_score": round(sentiment_score, 4),
            }

        except Exception as _sec_e:
            print(f"    ⚠️  [finbert] analyze_sections [{sec_key}] スコアリング失敗: "
                  f"{str(_sec_e)[:60]}")
            section_scores[sec_key] = {
                "label":           "NEUTRAL",
                "score":           0.5,
                "sentiment_score": 0.5,
            }

    return {
        "securities_code": sections_dict.get("securities_code", ""),
        "period_end":      sections_dict.get("period_end", ""),
        "section_scores":  section_scores,
        "analyzed_at":     datetime.now().isoformat(),
    }


def _get_or_init_pipeline():
    """
    モジュールレベルで FinBERT pipeline をシングルトン初期化して返す。
    既に初期化済みであればキャッシュを返す。
    """
    global _FINBERT_PIPELINE
    if _FINBERT_PIPELINE is not None:
        return _FINBERT_PIPELINE

    from transformers import pipeline as hf_pipeline
    _FINBERT_PIPELINE = hf_pipeline(
        "sentiment-analysis",
        model       = "ProsusAI/finbert",
        tokenizer   = "ProsusAI/finbert",
        return_all_scores = False,
        device      = -1,   # CPU実行（GPU未搭載環境対応）
    )
    return _FINBERT_PIPELINE

# ==============================================================
# BLOCK 3: 業種別センチメントスコア集計
# ==============================================================

def calc_sector_sentiment(
    articles_by_sector: dict,
    cfg = None,
) -> dict:
    """
    news_pipeline の by_sector データから業種別センチメントスコアを集計する。

    Args:
        articles_by_sector: news_pipeline["by_sector"] の dict
        cfg:                config モジュール

    Returns:
        {
          sector_code: {
            "avg_sentiment":   float,   # 0〜1（平均感情スコア）
            "positive_count":  int,
            "negative_count":  int,
            "neutral_count":   int,
            "article_count":   int,
            "top_positive":    [title, ...],
            "top_negative":    [title, ...],
          }
        }
    """
    result = {}

    for code, articles in articles_by_sector.items():
        scored = [a for a in articles if "sentiment_score" in a]
        if not scored:
            continue

        scores    = [a["sentiment_score"] for a in scored]
        pos_count = sum(1 for s in scores if s >= SENTIMENT_STRONG_POS)
        neg_count = sum(1 for s in scores if s <= SENTIMENT_STRONG_NEG)
        neu_count = len(scores) - pos_count - neg_count
        avg_sent  = sum(scores) / len(scores)

        # 上位記事（ポジティブ・ネガティブ）
        sorted_by_sent = sorted(scored, key=lambda x: x["sentiment_score"], reverse=True)
        top_positive = [a["title"][:60] for a in sorted_by_sent[:3]
                        if a.get("sentiment_label") == "POSITIVE"]
        top_negative = [a["title"][:60] for a in sorted_by_sent[-3:]
                        if a.get("sentiment_label") == "NEGATIVE"]

        result[code] = {
            "avg_sentiment":  round(avg_sent, 4),
            "positive_count": pos_count,
            "negative_count": neg_count,
            "neutral_count":  neu_count,
            "article_count":  len(scored),
            "top_positive":   top_positive,
            "top_negative":   top_negative,
        }

    return result


# ==============================================================
# BLOCK 4: センチメントトレンド計算
# ==============================================================

def calc_sentiment_trend(
    current_by_sector: dict,
    prev_cache: Optional[dict],
) -> dict:
    """
    前回キャッシュとの比較でセンチメントトレンド（improving/deteriorating/stable）を計算する。

    Args:
        current_by_sector : calc_sector_sentiment() の出力
        prev_cache         : 前回の sentiment_cache.json の by_sector

    Returns:
        current_by_sector に "sentiment_trend" を追加した dict
    """
    prev_by_sector = prev_cache.get("by_sector", {}) if prev_cache else {}

    for code, data in current_by_sector.items():
        curr_avg = data["avg_sentiment"]
        prev_avg = prev_by_sector.get(code, {}).get("avg_sentiment")

        if prev_avg is None:
            trend = "stable"
        elif curr_avg - prev_avg >= 0.05:
            trend = "improving"
        elif prev_avg - curr_avg >= 0.05:
            trend = "deteriorating"
        else:
            trend = "stable"

        data["sentiment_trend"] = trend

    return current_by_sector


# ==============================================================
# BLOCK 5: キャッシュ管理と LEADING_INDICATOR 統合
# ==============================================================

def _cache_path(cfg) -> Path:
    return Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / SENTIMENT_CACHE_FILE


def _load_cache(cfg) -> Optional[dict]:
    p = _cache_path(cfg)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        cached_at = datetime.fromisoformat(data.get("cached_at", "2000-01-01"))
        if datetime.now() - cached_at < timedelta(days=CACHE_VALID_DAYS):
            return data
    except Exception:
        pass
    return None


def _save_cache(cfg, cache: dict) -> None:
    p = _cache_path(cfg)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        cache["cached_at"] = datetime.now().isoformat()
        p.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"  ⚠️  sentiment_cache 保存失敗: {e}")


def build_sentiment_leading_signals(sentiment_by_sector: dict) -> list:
    """
    業種別センチメントから LEADING_INDICATOR_RULES 形式のシグナルを生成する。
    avg_sentiment > SENTIMENT_STRONG_POS: tailwind (+3pt)
    avg_sentiment < SENTIMENT_STRONG_NEG: headwind (-3pt)
    """
    signals = []
    for code, data in sentiment_by_sector.items():
        avg = data.get("avg_sentiment", 0.5)
        trend = data.get("sentiment_trend", "stable")

        if avg >= SENTIMENT_STRONG_POS:
            signal = "tailwind"
            pts    = SENTIMENT_TAILWIND_PTS
            if trend == "improving":
                pts += 1.0
        elif avg <= SENTIMENT_STRONG_NEG:
            signal = "headwind"
            pts    = SENTIMENT_HEADWIND_PTS
            if trend == "deteriorating":
                pts -= 1.0
        else:
            continue   # neutral はスキップ

        signals.append({
            "rule_id":       f"FINBERT_{code}",
            "label":         f"FinBERT感情({signal})",
            "sector_code":   code,
            "signal":        signal,
            "pts_adj":       round(pts, 1),
            "source":        "finbert",
            "avg_sentiment": avg,
            "trend":         trend,
        })

    return signals


def get_sector_sentiment(cfg, sector_code: str) -> Optional[dict]:
    """
    sentiment_cache.json から指定業種のセンチメントを返す。
    stock_screener.py や alert_notifier.py から参照する。
    """
    cache = _load_cache(cfg)
    if not cache:
        return None
    return cache.get("by_sector", {}).get(sector_code)


# ==============================================================
# BLOCK 5: メインエントリポイント
# ==============================================================

def run_finbert_analyzer(cfg, results: dict = None) -> dict:
    """
    FinBERT感情分析を実行する。

    stock_analyzer_main.py から WEEKLY_MODULES として呼び出す:
        _fb = _load_local_module("finbert_analyzer", ...)
        fb_result = _fb.run_finbert_analyzer(cfg, results)
        results["sentiment"] = fb_result
        # news_pipeline の by_sector を感情スコアで強化
        if results.get("news") and fb_result.get("by_sector"):
            results["news"]["sentiment"] = fb_result["by_sector"]
        # LEADING_INDICATOR_RULES に統合
        if fb_result.get("leading_signals"):
            existing = results.get("leading_indicator_signals", [])
            results["leading_indicator_signals"] = (
                existing + fb_result["leading_signals"]
            )

    処理:
      1. 7日以内のキャッシュがあれば再利用
      2. FinBERT パイプラインをロード（初回はDL・以降はキャッシュ）
      3. news_pipeline の articles に sentiment_score を付与
      4. 業種別集計 + トレンド計算
      5. LEADING_INDICATOR 形式シグナルに変換
      6. sentiment_cache.json に保存

    Args:
        cfg     : config モジュール
        results : run_all_tasks() の results dict
                  results["news"]["articles"] を入力として使用

    Returns:
        {
          "by_sector":      {code: sentiment_data},
          "leading_signals": [signal_dict, ...],
          "articles_scored": int,
          "model_used":     str,
          "cached":         bool,
          "timestamp":      str,
        }
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    if not getattr(cfg, "ENABLE_FINBERT", True):
        return {"skipped": True}

    print("\n  🧠 FinBERT感情分析 開始...")
    t0 = datetime.now()

    # ── Step1: キャッシュ確認 ─────────────────────────────────
    prev_cache = _load_cache(cfg)
    if prev_cache:
        n = len(prev_cache.get("by_sector", {}))
        print(f"    ✅ キャッシュ使用（{n}業種 / 有効期間内）")
        leading_signals = build_sentiment_leading_signals(
            prev_cache.get("by_sector", {})
        )
        return {
            "by_sector":       prev_cache.get("by_sector", {}),
            "leading_signals": leading_signals,
            "articles_scored": prev_cache.get("articles_scored", 0),
            "model_used":      prev_cache.get("model_used", ""),
            "cached":          True,
            "timestamp":       prev_cache.get("cached_at", ""),
        }

    # ── Step2: FinBERT パイプラインロード ────────────────────
    nlp, model_name = load_finbert_pipeline(cfg)
    if nlp is None:
        return {
            "skipped":  True,
            "reason":   "transformers 未インストール or モデルロード失敗",
            "by_sector": {},
            "leading_signals": [],
            "cached":   False,
        }

    # ── Step3: news_pipeline の articles に感情スコアを付与 ──
    results      = results or {}
    news_result  = results.get("news", {})
    articles     = news_result.get("articles", [])
    by_sector_articles = news_result.get("by_sector", {})

    if not articles:
        print("    ⚠️  news_pipeline の記事が0件 → FinBERT スキップ")
        return {
            "skipped":  True,
            "reason":   "news_pipeline 記事なし",
            "by_sector": {},
            "leading_signals": [],
        }

    batch_size = getattr(cfg, "FINBERT_BATCH_SIZE", DEFAULT_BATCH_SIZE)
    print(f"    📰 {len(articles)}件の記事を感情分析中（batch={batch_size}）...")

    enriched_articles = enrich_articles_with_sentiment(articles, nlp, batch_size)
    articles_scored   = sum(1 for a in enriched_articles if "sentiment_score" in a)

    # by_sector を更新（感情スコア付き記事で再構築）
    # news_pipeline の by_sector マッピングを維持しつつ感情スコアを反映
    enriched_by_sector = {}
    enriched_map = {a.get("url", ""): a for a in enriched_articles}
    for code, arts in by_sector_articles.items():
        enriched_by_sector[code] = [
            enriched_map.get(a.get("url", ""), a) for a in arts
        ]

    # ── Step4: 業種別センチメント集計 ────────────────────────
    sentiment_by_sector = calc_sector_sentiment(enriched_by_sector, cfg)
    sentiment_by_sector = calc_sentiment_trend(sentiment_by_sector, prev_cache)

    # ── Step5: LEADING_INDICATOR 形式シグナル ────────────────
    leading_signals = build_sentiment_leading_signals(sentiment_by_sector)

    # ── Step6: キャッシュ保存 ────────────────────────────────
    cache_data = {
        "by_sector":      sentiment_by_sector,
        "articles_scored": articles_scored,
        "model_used":     model_name,
    }
    _save_cache(cfg, cache_data)

    # news_pipeline の articles を感情スコア付きで上書き
    if results.get("news"):
        results["news"]["articles"] = enriched_articles

    elapsed   = (datetime.now() - t0).seconds
    tailwinds = sum(1 for s in leading_signals if s.get("signal") == "tailwind")
    headwinds = sum(1 for s in leading_signals if s.get("signal") == "headwind")

    print(
        f"    ✅ FinBERT完了（{elapsed}秒）: "
        f"{articles_scored}件スコアリング "
        f"| {len(sentiment_by_sector)}業種集計 "
        f"| 追い風{tailwinds}/逆風{headwinds}"
    )

    return {
        "by_sector":       sentiment_by_sector,
        "leading_signals": leading_signals,
        "articles_scored": articles_scored,
        "model_used":      model_name,
        "cached":          False,
        "timestamp":       datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
