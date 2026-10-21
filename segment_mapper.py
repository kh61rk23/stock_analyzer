# ==============================================================
# segment_mapper.py  v1.0  (E3)
# ==============================================================
# 【役割】
#   金融庁 EDINET 有報のセグメント名を東証33業種ラベルへ自動マッピングする。
#
# 【処理フロー】
#   1. sentence-transformers のコサイン類似度で33業種ラベルと比較
#      類似度 >= SEGMENT_VERIFIED_AUTO (0.90) → verified_flag=True で確定
#      類似度 >= SEGMENT_SIMILARITY_THRESHOLD (0.85) → 暫定確定
#      類似度 <  SEGMENT_SIMILARITY_THRESHOLD (0.85) → Gemini API に委譲
#   2. Gemini API 判定結果も確信度低い場合 → verified_flag=False で利用者確認キューへ
#   3. マッピング結果は CACHE_DIR/segment/segment_map_cache.json に保存
#      TTL: SEGMENT_MAPPER_TTL_DAYS (90日)
#
# 【BQ 生成リソース】
#   taxonomy.segments_master テーブル
#     segment_raw_name  STRING   有報原文のセグメント名
#     sector_code       STRING   マッピング先の33業種コード
#     sector_name_ja    STRING   業種名（日本語）
#     similarity_score  FLOAT64  コサイン類似度
#     mapped_by         STRING   "sentence_transformers" | "gemini" | "manual"
#     verified_flag     BOOL     利用者が確認済みか
#     review_queue      BOOL     確信度低→利用者確認キュー入り
#     created_at        TIMESTAMP
#     updated_at        TIMESTAMP
#
# 【依存】
#   config.py (BLOCK 35: GEMINI_API_KEY / SEGMENT_* 定数)
#   sentence-transformers (pip install sentence-transformers)
#   google-generativeai  (pip install google-generativeai)
#   edinet_bq_client.py  (BQ書き込み)
#
# 【呼出】
#   from segment_mapper import SegmentMapper
#   mapper = SegmentMapper(cfg)
#   result = mapper.map(segment_name)
#   # -> {"sector_code": "1016", "sector_name_ja": "電気機器",
#   #     "similarity_score": 0.92, "mapped_by": "sentence_transformers",
#   #     "verified_flag": True, "review_queue": False}
# ==============================================================

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

# GEMINI_API_KEY 未設定の警告を1実行1回に抑えるためのフラグ
_GEMINI_WARNED = False
from pathlib import Path
from typing import Optional

# ==============================================================
# BLOCK 1: 33業種ラベル定義（マッピング先）
# ==============================================================
# sentence-transformers のエンコード対象ラベル。
# config.JAPAN_SECTORS の name_ja / name_en に加え、
# 有報で頻出する別称・略称を列挙する。
# $similarity = cosine(embed(segment\_name), embed(label))$

_SECTOR_LABELS: list[dict] = [
    {"sector_code": "1001", "labels_ja": ["水産・農林業", "水産", "農業", "林業", "漁業", "農林水産"],
     "labels_en": ["fishery", "agriculture", "forestry", "fishing"]},
    {"sector_code": "1002", "labels_ja": ["鉱業", "石炭採掘", "金属鉱業", "非金属鉱物", "採掘"],
     "labels_en": ["mining", "coal mining", "metal mining"]},
    {"sector_code": "1003", "labels_ja": ["建設業", "建設", "土木", "建築", "住宅", "工事"],
     "labels_en": ["construction", "civil engineering", "building"]},
    {"sector_code": "1004", "labels_ja": ["食料品", "食品", "飲料", "食品製造", "農産品加工", "菓子"],
     "labels_en": ["food", "beverages", "food manufacturing"]},
    {"sector_code": "1005", "labels_ja": ["繊維製品", "繊維", "衣料", "アパレル", "テキスタイル"],
     "labels_en": ["textiles", "apparel", "clothing", "fashion"]},
    {"sector_code": "1006", "labels_ja": ["パルプ・紙", "紙", "パルプ", "印刷"],
     "labels_en": ["pulp", "paper", "printing"]},
    {"sector_code": "1007", "labels_ja": ["化学", "化学品", "石油化学", "合成樹脂", "化成品", "塗料"],
     "labels_en": ["chemicals", "chemical products", "petrochemicals"]},
    {"sector_code": "1008", "labels_ja": ["医薬品", "製薬", "バイオ", "創薬", "調剤", "医療用医薬品"],
     "labels_en": ["pharmaceutical", "drug", "biotech", "medicine"]},
    {"sector_code": "1009", "labels_ja": ["石油・石炭製品", "石油", "石炭", "エネルギー資源", "燃料"],
     "labels_en": ["oil", "coal products", "petroleum", "fuel"]},
    {"sector_code": "1010", "labels_ja": ["ゴム製品", "ゴム", "タイヤ", "合成ゴム"],
     "labels_en": ["rubber", "tires", "synthetic rubber"]},
    {"sector_code": "1011", "labels_ja": ["ガラス・土石製品", "ガラス", "セメント", "陶磁器", "窯業"],
     "labels_en": ["glass", "ceramics", "cement", "stone"]},
    {"sector_code": "1012", "labels_ja": ["鉄鋼", "鉄", "鋼", "高炉", "電炉", "特殊鋼"],
     "labels_en": ["iron", "steel", "blast furnace"]},
    {"sector_code": "1013", "labels_ja": ["非鉄金属", "銅", "アルミ", "亜鉛", "チタン", "レアメタル"],
     "labels_en": ["nonferrous metals", "copper", "aluminum", "zinc"]},
    {"sector_code": "1014", "labels_ja": ["金属製品", "金属加工", "鋳造", "プレス", "ばね", "ファスナー"],
     "labels_en": ["metal products", "metalwork", "fabricated metals"]},
    {"sector_code": "1015", "labels_ja": ["機械", "産業機械", "工作機械", "ロボット", "農業機械"],
     "labels_en": ["machinery", "industrial machinery", "robots"]},
    {"sector_code": "1016", "labels_ja": ["電気機器", "電機", "半導体", "電子部品", "家電", "重電"],
     "labels_en": ["electric appliances", "semiconductor", "electronics"]},
    {"sector_code": "1017", "labels_ja": ["輸送用機器", "自動車", "二輪車", "鉄道車両", "船舶", "航空機"],
     "labels_en": ["transportation equipment", "automobile", "vehicles"]},
    {"sector_code": "1018", "labels_ja": ["精密機器", "光学機器", "医療機器", "計測機器", "カメラ"],
     "labels_en": ["precision instruments", "optics", "medical devices"]},
    {"sector_code": "1019", "labels_ja": ["その他製品", "家具", "スポーツ用品", "玩具", "楽器", "印刷業"],
     "labels_en": ["other products", "furniture", "sporting goods", "toys"]},
    {"sector_code": "1020", "labels_ja": ["電気・ガス業", "電力", "ガス", "発電", "送電"],
     "labels_en": ["electric power", "gas", "utility", "electricity"]},
    {"sector_code": "1021", "labels_ja": ["陸運業", "鉄道", "バス", "トラック", "宅配", "物流"],
     "labels_en": ["land transportation", "railway", "trucking", "logistics"]},
    {"sector_code": "1022", "labels_ja": ["海運業", "船舶輸送", "内航", "外航", "タンカー"],
     "labels_en": ["marine transportation", "shipping", "tanker"]},
    {"sector_code": "1023", "labels_ja": ["空運業", "航空", "エアライン", "貨物航空"],
     "labels_en": ["air transportation", "airline", "aviation"]},
    {"sector_code": "1024", "labels_ja": ["倉庫・運輸関連業", "倉庫", "港湾", "フォワーダー", "荷役"],
     "labels_en": ["warehousing", "harbor transportation", "freight"]},
    {"sector_code": "1025", "labels_ja": ["情報・通信業", "IT", "ソフトウェア", "通信", "インターネット"],
     "labels_en": ["information", "communication", "software", "internet"]},
    {"sector_code": "1026", "labels_ja": ["卸売業", "商社", "卸", "総合商社", "専門商社"],
     "labels_en": ["wholesale", "trading company", "distributor"]},
    {"sector_code": "1027", "labels_ja": ["小売業", "小売", "スーパー", "コンビニ", "百貨店", "EC"],
     "labels_en": ["retail", "supermarket", "convenience store", "e-commerce"]},
    {"sector_code": "1028", "labels_ja": ["銀行業", "銀行", "地方銀行", "メガバンク", "信用金庫"],
     "labels_en": ["banks", "banking", "regional bank", "megabank"]},
    {"sector_code": "1029", "labels_ja": ["証券・商品先物取引業", "証券", "先物", "FX", "投資銀行"],
     "labels_en": ["securities", "brokerage", "commodity futures"]},
    {"sector_code": "1030", "labels_ja": ["保険業", "生命保険", "損害保険", "再保険"],
     "labels_en": ["insurance", "life insurance", "non-life insurance"]},
    {"sector_code": "1031", "labels_ja": ["その他金融業", "リース", "消費者金融", "クレジット", "ノンバンク"],
     "labels_en": ["other financing", "leasing", "consumer finance", "credit"]},
    {"sector_code": "1032", "labels_ja": ["不動産業", "不動産", "REIT", "マンション", "賃貸"],
     "labels_en": ["real estate", "REIT", "property", "housing"]},
    {"sector_code": "1033", "labels_ja": ["サービス業", "サービス", "外食", "人材", "広告", "エンタメ"],
     "labels_en": ["services", "restaurant", "staffing", "advertising"]},
]


# ==============================================================
# BLOCK 2: キャッシュ管理
# ==============================================================

def _get_cache_path(cfg) -> Path:
    """segment_map_cache.json のパスを返す。"""
    base = Path(getattr(cfg, "CACHE_DIR", "stock_data/cache"))
    d = base / "segment"
    d.mkdir(parents=True, exist_ok=True)
    return d / "segment_map_cache.json"


def _load_cache(cfg) -> dict:
    """
    キャッシュ読み込み。TTL切れエントリは削除して返す。
    {segment_raw_name: {sector_code, ..., cached_at}, ...}
    """
    path = _get_cache_path(cfg)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

    ttl_days = getattr(cfg, "SEGMENT_MAPPER_TTL_DAYS", 90)
    cutoff = datetime.now() - timedelta(days=ttl_days)
    cleaned = {}
    for name, entry in data.items():
        cached_at_str = entry.get("cached_at", "")
        try:
            cached_at = datetime.fromisoformat(cached_at_str)
            if cached_at >= cutoff:
                cleaned[name] = entry
        except Exception:
            pass   # パース失敗エントリは削除扱い
    return cleaned


def _save_cache(cfg, cache: dict) -> None:
    """キャッシュをJSONに保存する。失敗時は警告ログのみ。"""
    path = _get_cache_path(cfg)
    try:
        path.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"    ⚠️  [segment_mapper] キャッシュ保存失敗: {e}")


# ==============================================================
# BLOCK 3: sentence-transformers エンコーダ（シングルトン）
# ==============================================================

_model_cache: dict = {}   # {"model": SentenceTransformer インスタンス}


def _get_st_model():
    """
    SentenceTransformer モデルをシングルトンで返す。
    未インストール時は ImportError を再 raise する。
    モデル: paraphrase-multilingual-MiniLM-L12-v2
      - 日英バイリンガル対応・軽量（118MB）・商用利用可
    """
    if "model" not in _model_cache:
        try:
            from sentence_transformers import SentenceTransformer
            print("    📦 [segment_mapper] sentence-transformers モデルロード中...")
            _model_cache["model"] = SentenceTransformer(
                "paraphrase-multilingual-MiniLM-L12-v2"
            )
            print("    ✅ [segment_mapper] モデルロード完了")
        except ImportError as e:
            raise ImportError(
                "sentence-transformers が未インストールです。"
                "`pip install sentence-transformers` を実行してください。"
            ) from e
    return _model_cache["model"]


def _build_label_embeddings(model) -> tuple[list[str], list, list[str]]:
    """
    _SECTOR_LABELS の全ラベルを結合してエンコードする。

    Returns:
        (all_labels, embeddings, sector_codes)
        all_labels[i] のエンコード結果が embeddings[i]、
        対応する sector_code が sector_codes[i]。
    """
    all_labels: list[str]   = []
    sector_codes: list[str] = []

    for entry in _SECTOR_LABELS:
        code = entry["sector_code"]
        for lbl in entry["labels_ja"] + entry["labels_en"]:
            all_labels.append(lbl)
            sector_codes.append(code)

    embeddings = model.encode(all_labels, convert_to_tensor=True)
    return all_labels, embeddings, sector_codes


_label_embed_cache: dict = {}   # {"labels": ..., "embeddings": ..., "codes": ...}


def _get_label_embeddings(model):
    """ラベルエンコードをキャッシュして返す（セッション中に1回のみ計算）。"""
    if not _label_embed_cache:
        labels, embs, codes = _build_label_embeddings(model)
        _label_embed_cache["labels"]     = labels
        _label_embed_cache["embeddings"] = embs
        _label_embed_cache["codes"]      = codes
    return (
        _label_embed_cache["labels"],
        _label_embed_cache["embeddings"],
        _label_embed_cache["codes"],
    )


# ==============================================================
# BLOCK 4: コサイン類似度でのマッピング
# ==============================================================

def _map_by_sentence_transformers(
    segment_name: str,
    cfg,
) -> Optional[dict]:
    """
    sentence-transformers でセグメント名を33業種にマッピングする。

    Returns:
        {
          "sector_code":      str,
          "sector_name_ja":   str,
          "similarity_score": float,
          "mapped_by":        "sentence_transformers",
          "verified_flag":    bool,   # True if score >= SEGMENT_VERIFIED_AUTO
          "review_queue":     bool,   # True if score < SEGMENT_SIMILARITY_THRESHOLD
        }
        None: モデルロード失敗時
    """
    try:
        import torch
        model  = _get_st_model()
        labels, label_embs, codes = _get_label_embeddings(model)

        seg_emb = model.encode([segment_name], convert_to_tensor=True)

        # コサイン類似度計算（全ラベルとの類似度ベクトル）
        from sentence_transformers import util as st_util
        cos_scores = st_util.cos_sim(seg_emb, label_embs)[0]   # shape: (N,)

        # 最大スコアのインデックス
        best_idx   = int(cos_scores.argmax())
        best_score = float(cos_scores[best_idx])
        best_code  = codes[best_idx]

        # 業種名取得
        sector_name_ja = next(
            (e["labels_ja"][0] for e in _SECTOR_LABELS if e["sector_code"] == best_code),
            best_code,
        )

        thresh_auto    = getattr(cfg, "SEGMENT_VERIFIED_AUTO",         0.90)
        thresh_confirm = getattr(cfg, "SEGMENT_SIMILARITY_THRESHOLD",  0.85)

        verified   = best_score >= thresh_auto
        review     = best_score < thresh_confirm

        return {
            "sector_code":      best_code,
            "sector_name_ja":   sector_name_ja,
            "similarity_score": round(best_score, 4),
            "mapped_by":        "sentence_transformers",
            "verified_flag":    verified,
            "review_queue":     review,
        }

    except Exception as e:
        print(f"    ⚠️  [segment_mapper] ST マッピングエラー ({segment_name}): {e}")
        return None


# ==============================================================
# BLOCK 5: Gemini API でのマッピング（フォールバック）
# ==============================================================

def _map_by_gemini(
    segment_name: str,
    cfg,
    retry: int = 2,
) -> Optional[dict]:
    """
    Gemini API（無料枠・1日1500リクエスト）でセグメント名を判定する。
    sentence-transformers の類似度が SEGMENT_SIMILARITY_THRESHOLD 未満の
    場合のみ呼び出す（コスト・レート制限対策）。

    Gemini に JSON 形式で返答させ、sector_code と confidence を抽出する。
    confidence < 0.7 の場合は verified_flag=False / review_queue=True。

    Returns:
        {
          "sector_code":      str,
          "sector_name_ja":   str,
          "similarity_score": float,   # Gemini の confidence を代入
          "mapped_by":        "gemini",
          "verified_flag":    bool,
          "review_queue":     bool,
        }
        None: API キー未設定・通信失敗時
    """
    api_key = getattr(cfg, "GEMINI_API_KEY", "")
    if not api_key:
        # セグメントごとに出すと同じ警告が並び他の警告を埋もれさせる。
        # 未設定は実行中に変わらないので1回だけ出す。
        global _GEMINI_WARNED
        if not _GEMINI_WARNED:
            print("    ⚠️  [segment_mapper] GEMINI_API_KEY 未設定 → Gemini スキップ（以降は表示省略）")
            _GEMINI_WARNED = True
        return None

    # 業種リスト文字列（プロンプト用）
    sector_list = "\n".join(
        f"  {e['sector_code']}: {e['labels_ja'][0]}"
        for e in _SECTOR_LABELS
    )

    prompt = (
        "以下の有価証券報告書のセグメント名を、東証33業種コードに分類してください。\n"
        "必ず JSON のみを返してください（説明文・マークダウン不要）。\n"
        "形式: {\"sector_code\": \"1016\", \"confidence\": 0.92}\n\n"
        f"セグメント名: 「{segment_name}」\n\n"
        "東証33業種コード一覧:\n"
        f"{sector_list}"
    )

    model_name = getattr(cfg, "SEGMENT_GEMINI_MODEL", "gemini-1.5-flash")

    for attempt in range(1, retry + 1):
        try:
            import google.generativeai as genai
            genai.configure(api_key=api_key)
            model  = genai.GenerativeModel(model_name)
            resp   = model.generate_content(prompt)
            raw    = resp.text.strip()

            # JSON 抽出（```json ... ``` フェンスの除去）
            if "```" in raw:
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            data = json.loads(raw.strip())

            sector_code = str(data.get("sector_code", "")).strip()
            confidence  = float(data.get("confidence", 0.0))

            # sector_code の存在チェック
            valid_codes = {e["sector_code"] for e in _SECTOR_LABELS}
            if sector_code not in valid_codes:
                print(
                    f"    ⚠️  [segment_mapper] Gemini 返答の sector_code 不正: "
                    f"'{sector_code}'"
                )
                return None

            sector_name_ja = next(
                (e["labels_ja"][0] for e in _SECTOR_LABELS
                 if e["sector_code"] == sector_code),
                sector_code,
            )

            return {
                "sector_code":      sector_code,
                "sector_name_ja":   sector_name_ja,
                "similarity_score": round(confidence, 4),
                "mapped_by":        "gemini",
                "verified_flag":    confidence >= 0.85,
                "review_queue":     confidence < 0.70,
            }

        except json.JSONDecodeError as je:
            print(
                f"    ⚠️  [segment_mapper] Gemini JSON パース失敗 "
                f"(attempt {attempt}): {je}"
            )
        except Exception as e:
            print(
                f"    ⚠️  [segment_mapper] Gemini API エラー "
                f"(attempt {attempt}): {str(e)[:80]}"
            )
            if attempt < retry:
                time.sleep(2 ** attempt)   # 指数バックオフ: 2s, 4s

    return None


# ==============================================================
# BLOCK 6: BQ 書き込み（taxonomy.segments_master）
# ==============================================================

def _write_to_bq(cfg, result: dict, segment_raw_name: str) -> None:
    """
    マッピング結果を BigQuery の taxonomy.segments_master へ Upsert する。
    BQ 未接続・スキーマ不一致などのエラーは全て try/except で吸収し、
    システム全停止を防ぐ。

    BQ テーブルスキーマ:
      segment_raw_name  STRING    有報原文のセグメント名（主キー相当）
      sector_code       STRING    マッピング先33業種コード
      sector_name_ja    STRING    業種名（日本語）
      similarity_score  FLOAT64   コサイン類似度（または Gemini confidence）
      mapped_by         STRING    "sentence_transformers" | "gemini" | "manual"
      verified_flag     BOOL      確認済みフラグ
      review_queue      BOOL      利用者確認キュー入りフラグ
      created_at        TIMESTAMP 初回登録日時
      updated_at        TIMESTAMP 最終更新日時
    """
    try:
        import sys as _sys, importlib as _imp, pathlib as _pl
        _sys.path.insert(0, str(_pl.Path(__file__).parent.parent))
        _bq_mod = _imp.import_module("edinet_bq_client")
        bq = _bq_mod.EDINETBQClient()

        # datetime.utcnow() は Python 3.12 で非推奨（tz-naive を返すため
        # 後段で aware な値と比較すると TypeError になる）。
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        row = {
            "segment_raw_name": segment_raw_name,
            "sector_code":      result["sector_code"],
            "sector_name_ja":   result["sector_name_ja"],
            "similarity_score": result["similarity_score"],
            "mapped_by":        result["mapped_by"],
            "verified_flag":    result["verified_flag"],
            "review_queue":     result["review_queue"],
            "updated_at":       now_str,
        }

        # EDINETBQClient に upsert_segment_master() がある場合は使用
        # なければ insert_rows() にフォールバック
        if hasattr(bq, "upsert_segment_master"):
            bq.upsert_segment_master(row)
        elif hasattr(bq, "insert_rows"):
            bq.insert_rows("taxonomy.segments_master", [row])
        else:
            print(
                "    ⚠️  [segment_mapper] BQ クライアントに書き込みメソッドなし → スキップ"
            )
    except Exception as e:
        # BQ 書き込み失敗はキャッシュで補完できるため警告のみ
        print(f"    ⚠️  [segment_mapper] BQ 書き込みエラー（スキップ）: {str(e)[:80]}")


# ==============================================================
# BLOCK 7: メインマッパークラス
# ==============================================================

class SegmentMapper:
    """
    有報セグメント名 → 東証33業種コードへのマッパー。

    使い方:
        mapper = SegmentMapper(cfg)
        result = mapper.map("半導体・電子部品事業")
        # -> {"sector_code": "1016", "sector_name_ja": "電気機器",
        #     "similarity_score": 0.93, "mapped_by": "sentence_transformers",
        #     "verified_flag": True, "review_queue": False}

        # 複数一括処理
        results = mapper.map_batch(["食品事業", "物流事業", "???事業"])

        # 確認キュー（利用者が手動確認すべきリスト）を表示
        mapper.print_review_queue()
    """

    def __init__(self, cfg):
        self._cfg           = cfg
        self._cache: dict   = _load_cache(cfg)
        self._review_queue: list[dict] = []   # 確認待ちエントリ

    def map(self, segment_name: str, write_bq: bool = False) -> dict:
        """
        セグメント名を1件マッピングする。

        Args:
            segment_name: 有報のセグメント名（生文字列）
            write_bq    : True のとき BQ の segments_master に書き込む

        Returns:
            マッピング結果辞書。失敗時は fallback（verified_flag=False）を返す。
        """
        name = segment_name.strip()
        if not name:
            return self._fallback(name, "empty input")

        # ── キャッシュヒット ────────────────────────────────────
        if name in self._cache:
            entry = self._cache[name]
            return {k: v for k, v in entry.items() if k != "cached_at"}

        # ── Step 1: sentence-transformers ───────────────────────
        result = _map_by_sentence_transformers(name, self._cfg)

        thresh = getattr(self._cfg, "SEGMENT_SIMILARITY_THRESHOLD", 0.85)

        # ── Step 2: 類似度不足なら Gemini に委譲 ─────────────────
        if result is None or result["similarity_score"] < thresh:
            st_result  = result   # ST の結果は参考保持
            gem_result = _map_by_gemini(name, self._cfg)

            if gem_result is not None:
                result = gem_result
                # ST 類似度も参考情報として付記
                if st_result is not None:
                    result["st_similarity_score"] = st_result["similarity_score"]
            elif st_result is not None:
                # Gemini も失敗 → ST の結果を使いつつ review_queue=True
                result = st_result
                result["review_queue"] = True
                result["verified_flag"] = False
            else:
                # 両方失敗
                return self._fallback(name, "both ST and Gemini failed")

        # ── レビューキュー登録 ─────────────────────────────────
        if result.get("review_queue"):
            self._review_queue.append({
                "segment_raw_name": name,
                **result,
                "queued_at": datetime.now().isoformat(),
            })

        # ── キャッシュ保存 ─────────────────────────────────────
        self._cache[name] = {**result, "cached_at": datetime.now().isoformat()}
        _save_cache(self._cfg, self._cache)

        # ── BQ 書き込み（オプション） ───────────────────────────
        if write_bq:
            _write_to_bq(self._cfg, result, name)

        return result

    def map_batch(
        self,
        segment_names: list[str],
        write_bq: bool = False,
        sleep_sec: float = 0.3,
    ) -> list[dict]:
        """
        複数セグメント名を一括マッピングする。

        Args:
            segment_names: セグメント名リスト
            write_bq     : True のとき各結果を BQ に書き込む
            sleep_sec    : Gemini レート制限対策のスリープ秒数

        Returns:
            [{"segment_raw_name": str, **map_result}, ...] の順序保証リスト
        """
        results = []
        for name in segment_names:
            r = self.map(name, write_bq=write_bq)
            results.append({"segment_raw_name": name, **r})
            # Gemini を使用した場合のみスリープ（キャッシュヒットは不要）
            if r.get("mapped_by") == "gemini":
                time.sleep(sleep_sec)
        return results

    def print_review_queue(self) -> None:
        """確認キューをコンソールに出力する（利用者への確認依頼用）。"""
        if not self._review_queue:
            print("  ✅ [segment_mapper] 確認キュー: 0件（全マッピング確定済み）")
            return
        print(
            f"\n  ⚠️  [segment_mapper] 確認キュー: "
            f"{len(self._review_queue)}件（手動確認が必要）"
        )
        print("  " + "-" * 60)
        for i, entry in enumerate(self._review_queue, 1):
            name   = entry.get("segment_raw_name", "")
            code   = entry.get("sector_code", "?")
            sname  = entry.get("sector_name_ja", "")
            score  = entry.get("similarity_score", 0)
            by     = entry.get("mapped_by", "?")
            print(
                f"  [{i:02d}] 「{name}」 → "
                f"{code} {sname} "
                f"(score={score:.3f}, by={by})"
            )
        print("  " + "-" * 60)

    def get_review_queue(self) -> list[dict]:
        """確認キューをリストで返す（stock_analyzer_main からの参照用）。"""
        return list(self._review_queue)

    @staticmethod
    def _fallback(name: str, reason: str) -> dict:
        """マッピング失敗時のフォールバック辞書を返す。"""
        print(f"    ⚠️  [segment_mapper] フォールバック ({name}): {reason}")
        return {
            "sector_code":      "",
            "sector_name_ja":   "不明",
            "similarity_score": 0.0,
            "mapped_by":        "fallback",
            "verified_flag":    False,
            "review_queue":     True,
        }


# ==============================================================
# BLOCK 8: モジュールレベル便利関数
# ==============================================================

def map_segment(segment_name: str, cfg, write_bq: bool = False) -> dict:
    """
    SegmentMapper をインスタンス化せずに1件マッピングできる便利関数。
    base_agent.py や segment_signal.py から呼び出す際に使用。

    Args:
        segment_name: 有報のセグメント名
        cfg         : config モジュール
        write_bq    : True のとき BQ の segments_master に書き込む

    Returns:
        map() と同じ辞書
    """
    mapper = SegmentMapper(cfg)
    return mapper.map(segment_name, write_bq=write_bq)


def batch_map_from_edinet_db(cfg, tickers: list[str]) -> dict:
    """
    edinet_bq_client から対象銘柄のセグメント名を取得し、
    一括マッピングして結果を返す。

    Args:
        cfg    : config モジュール
        tickers: 対象銘柄ティッカーリスト（例: ["6758.T", "9984.T"]）

    Returns:
        {
          ticker: [
            {"segment_raw_name": str, "sector_code": str, ...},
            ...
          ],
          ...
        }
    """
    results: dict = {}
    mapper = SegmentMapper(cfg)

    for ticker in tickers:
        try:
            import sys as _sys, importlib as _imp, pathlib as _pl
            _sys.path.insert(0, str(_pl.Path(__file__).parent.parent))
            bq_mod = _imp.import_module("edinet_bq_client")
            bq = bq_mod.EDINETBQClient()

            # セグメント名取得（edinet_bq_client に get_segments() がある前提）
            if hasattr(bq, "get_segments"):
                segments = bq.get_segments(ticker)   # [{"segment_name": str, ...}]
            else:
                segments = []

            if not segments:
                results[ticker] = []
                continue

            seg_names = [s.get("segment_name", "") for s in segments if s.get("segment_name")]
            mapped    = mapper.map_batch(seg_names, write_bq=True)
            results[ticker] = mapped

        except Exception as e:
            print(
                f"    ⚠️  [segment_mapper] {ticker} のセグメント取得エラー: "
                f"{str(e)[:80]}"
            )
            results[ticker] = []

    mapper.print_review_queue()
    return results


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

    print("=== segment_mapper 単体テスト ===")
    mapper = SegmentMapper(cfg)

    test_cases = [
        "半導体・電子デバイス事業",
        "食品事業",
        "不動産開発事業",
        "金融サービス事業",
        "自動車部品製造",
        "エネルギーソリューション",
        "???謎のセグメント名XYZ???",   # 低類似度テスト
    ]

    print("\n  マッピング結果:")
    for seg in test_cases:
        r = mapper.map(seg)
        verified = "✅" if r["verified_flag"] else ("🔍" if r["review_queue"] else "⚠️")
        print(
            f"  {verified} 「{seg}」 → "
            f"{r['sector_code']} {r['sector_name_ja']} "
            f"(score={r['similarity_score']:.3f}, by={r['mapped_by']})"
        )

    mapper.print_review_queue()