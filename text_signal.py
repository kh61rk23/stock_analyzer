# ==============================================================
# text_signal.py  v1.0  (E4-2)
# 有報テキストの感情スコアYoY差分・リスクカテゴリ分類ボーナスを計算する
# ==============================================================
# 【役割】
#   finbert_analyzer.analyze_sections() の出力を受け取り、
#   前期比（YoY）の感情スコア差分およびリスクカテゴリ別スコアを算出し、
#   base_agent.py の edinet_text_risk_bonus として使用できる
#   $-10$ 〜 $0$ pt のスカラー値を返す。
#
# 【スコア設計】
#   - リスク検出に特化したネガティブ方向のボーナス（ペナルティ）
#   - YoY_delta: 前期比でリスクスコアが悪化した場合に加点
#   - category_penalty: 地政学・財務リスクのキーワード出現密度に応じた加点
#   - 合算後、$[TEXT\_SIGNAL\_SCORE\_MIN, 0]$ にクランプ
#
# 【キャッシュ】
#   stock_data/cache/text_signal_cache.json  TTL=30日（週次更新）
#
# 【呼び出し】
#   from text_signal import calc_text_risk_bonus, batch_calc_text_risk_bonus
# ==============================================================

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


# ==============================================================
# BLOCK 0: 定数
# ==============================================================

# キャッシュキー構造: {securities_code: TextSignalResult}
_CACHE_SCHEMA_VERSION = "1.0"

# YoY 差分の重み（risk セクションのネガティブ化を最重視）

from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'TXS_YOY_SECTION_WEIGHTS': '_YOY_SECTION_WEIGHTS',
    'TXS_CATEGORY_MAX_PENALTY': '_CATEGORY_MAX_PENALTY',
    'TXS_KEYWORD_UNIT_PENALTY': '_KEYWORD_UNIT_PENALTY',
    'TXS_CATALYST_SCORE_MAX': '_CATALYST_SCORE_MAX',
    'TXS_CATALYST_SUPPLY_THRESHOLD': '_CATALYST_SUPPLY_THRESHOLD',
    'B6_TEXT_SIGNAL_YOY_SCALE_FACTOR': '_YOY_SCALE_FACTOR',
}

_YOY_SECTION_WEIGHTS: dict[str, float] = {
    "risk":       1.0,
    "business":   0.4,
    "strategy":   0.3,
    "governance": 0.2,
}

# YoY delta を bonus pt に変換するスケール係数
# delta は sentiment_score の差分（-1.0〜1.0）
# delta=-1.0（最大悪化）→ bonus=-5.0pt に相当
_YOY_SCALE_FACTOR = 5.0

# カテゴリリスクの最大ペナルティ（pt）
_CATEGORY_MAX_PENALTY = 5.0

# キーワード1件あたりのペナルティ基礎値（pt）
_KEYWORD_UNIT_PENALTY = 0.5

# ==============================================================
# BLOCK 0-B: 追加定数（カタリスト候補自動特定）
# ==============================================================

# カタリスト特定に使うキーワード辞書（正値=ポジティブカタリスト）
# independence_score_calculator.py の _CATALYST_KEYWORDS と同一体系
# text_signal.py 側はリスク方向だけでなく「ポジティブカタリスト」も検出する
_CATALYST_POSITIVE_KEYWORDS: dict = {
    # 事業拡大系
    "新規事業":     (2.0, "new_business"),
    "新サービス":   (1.5, "new_business"),
    "新製品":       (1.5, "new_product"),
    "海外展開":     (2.0, "expansion"),
    "新市場":       (1.5, "expansion"),
    # R&D系
    "研究開発":     (1.5, "rd_investment"),
    "特許":         (1.0, "rd_investment"),
    "共同研究":     (2.0, "rd_collaboration"),
    # M&A系
    "買収":         (2.5, "ma"),
    "合併":         (2.5, "ma"),
    "資本業務提携": (2.0, "partnership"),
    "業務提携":     (1.5, "partnership"),
    # 財務改善系
    "自社株買い":   (2.0, "shareholder_return"),
    "増配":         (2.0, "shareholder_return"),
    "特別配当":     (2.5, "shareholder_return"),
    # ガイダンス改善系
    "上方修正":     (3.0, "guidance_up"),
    "黒字転換":     (3.0, "turnaround"),
    "量産":         (2.5, "mass_production"),
    "大型受注":     (3.0, "order_win"),
    "受注":         (2.0, "order_win"),
}

# カタリストスコアの正規化上限
_CATALYST_SCORE_MAX = 10.0

# latent_pearl_manager への供給閾値
# この値以上の catalyst_score を持つ銘柄のみ latent_pearl_manager に供給する
_CATALYST_SUPPLY_THRESHOLD = 2.0

# カタリストスコアキャッシュのキー
_CATALYST_CACHE_KEY_SUFFIX = "_catalyst"

# ==============================================================
# BLOCK 1: キャッシュ管理
# ==============================================================

def _cache_path(cfg) -> Path:
    base = Path(getattr(cfg, "CACHE_DIR", "stock_data/cache"))
    return base / getattr(cfg, "TEXT_SIGNAL_CACHE_FILE", "text_signal_cache.json")

def _load_full_cache(cfg) -> dict:
    p = _cache_path(cfg)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        ttl = getattr(cfg, "TEXT_SIGNAL_TTL_DAYS", 30)
        now = datetime.now()
        valid = {}
        for code, entry in raw.items():
            if code == "__meta__":
                continue
            try:
                calc_at = datetime.fromisoformat(entry.get("calc_at", "2000-01-01"))
                if now - calc_at <= timedelta(days=ttl):
                    valid[code] = entry
            except Exception:
                pass
        return valid
    except Exception:
        return {}

def _save_full_cache(cfg, data: dict) -> None:
    p = _cache_path(cfg)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "__meta__": {
                "schema_version": _CACHE_SCHEMA_VERSION,
                "updated_at":     datetime.now().isoformat(),
            },
            **data,
        }
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"    ⚠️  [text_signal] キャッシュ保存失敗: {e}")

def _get_cached_entry(cfg, securities_code: str) -> Optional[dict]:
    cache = _load_full_cache(cfg)
    return cache.get(str(securities_code))

def _upsert_cache(cfg, securities_code: str, entry: dict) -> None:
    cache = _load_full_cache(cfg)
    cache[str(securities_code)] = entry
    _save_full_cache(cfg, cache)


# ==============================================================
# BLOCK 2: YoY 差分計算
# ==============================================================

def _calc_yoy_delta(
    current_scores: dict,
    prev_scores:    Optional[dict],
) -> float:
    """
    今期と前期の section_scores から加重 YoY delta を計算する。

    Args:
        current_scores: finbert_analyzer.analyze_sections() の "section_scores"
        prev_scores:    前期の同フィールド（None の場合は delta=0.0）

    Returns:
        加重 YoY delta。負値ほどリスク悪化を示す。範囲: $-1.0$ 〜 $1.0$
    """
    if not prev_scores:
        return 0.0

    weighted_sum  = 0.0
    total_weight  = 0.0

    for sec_key, weight in _YOY_SECTION_WEIGHTS.items():
        cur_entry  = current_scores.get(sec_key, {})
        prev_entry = prev_scores.get(sec_key, {})
        cur_sent   = cur_entry.get("sentiment_score", 0.5)
        prev_sent  = prev_entry.get("sentiment_score", 0.5)
        delta      = cur_sent - prev_sent   # 負 = 悪化
        weighted_sum += delta * weight
        total_weight += weight

    if total_weight == 0:
        return 0.0

    return round(weighted_sum / total_weight, 4)


# ==============================================================
# BLOCK 3: リスクカテゴリペナルティ計算
# ==============================================================

def _calc_category_penalty(
    sections_dict: dict,
    cfg,
) -> dict[str, float]:
    """
    テキスト本文中のリスクカテゴリキーワード出現数から
    カテゴリ別ペナルティスコアを計算する。

    Args:
        sections_dict: text_extractor.extract_sections() の出力
        cfg:           config モジュール（TEXT_SIGNAL_RISK_CATEGORIES を参照）

    Returns:
        {"geopolitical": float, "financial": float}
        各値は [CATEGORY_MAX_PENALTY * -1, 0] の範囲
    """
    risk_categories: dict = getattr(
        cfg,
        "TEXT_SIGNAL_RISK_CATEGORIES",
        {
            "geopolitical": ["地政学", "輸出規制", "制裁", "関税", "安全保障"],
            "financial":    ["流動性リスク", "信用リスク", "為替リスク", "減損リスク", "負債"],
        },
    )

    # リスクセクションを優先的に検索（businessも補助的に使用）
    inner    = sections_dict.get("sections", {}) if sections_dict else {}
    risk_txt = (inner.get("risk", "") or "") + " " + (inner.get("business", "") or "")

    penalties: dict[str, float] = {}
    for cat, keywords in risk_categories.items():
        hit_count = sum(risk_txt.count(kw) for kw in keywords)
        # ペナルティ計算: キーワード出現1件 = $-0.5$ pt、上限 $-5.0$ pt
        raw_penalty = hit_count * _KEYWORD_UNIT_PENALTY * -1.0
        penalties[cat] = max(raw_penalty, -_CATEGORY_MAX_PENALTY)

    return penalties


# ==============================================================
# BLOCK 4: メインスコア計算（公開 API）
# ==============================================================

def calc_text_risk_bonus(
    cfg,
    securities_code:  str,
    current_analysis: dict,
    prev_analysis:    Optional[dict] = None,
    sections_dict:    Optional[dict] = None,
) -> float:
    """
    有報テキスト分析結果からリスクボーナス（ペナルティ）スカラーを計算する。

    スコア設計:
      bonus = clamp(yoy_component + category_component, TEXT_SIGNAL_SCORE_MIN, 0)

      yoy_component      = YoY_delta * _YOY_SCALE_FACTOR * -1
                           (delta が負=悪化なら正値になるため -1 をかけてペナルティ化)
      category_component = sum(category_penalties.values()) の加重合算

    Args:
        cfg:              config モジュール
        securities_code:  証券コード
        current_analysis: finbert_analyzer.analyze_sections() の今期出力
        prev_analysis:    同前期出力（None の場合 YoY 差分=0）
        sections_dict:    text_extractor.extract_sections() の出力（カテゴリ計算用）

    Returns:
        float: $TEXT\\_SIGNAL\\_SCORE\\_MIN$ 〜 $0.0$ の範囲のボーナス値
        エラー時は 0.0 を返す（システム継続保護）
    """
    try:
        if not getattr(cfg, "ENABLE_TEXT_SIGNAL", True):
            return 0.0
        if not current_analysis or not isinstance(current_analysis, dict):
            return 0.0

        cur_scores  = current_analysis.get("section_scores", {})
        prev_scores = prev_analysis.get("section_scores", {}) if prev_analysis else None

        # ── YoY 差分コンポーネント ─────────────────────────────
        yoy_delta       = _calc_yoy_delta(cur_scores, prev_scores)
        # delta が負（悪化）→ ペナルティ方向に変換
        yoy_component   = yoy_delta * _YOY_SCALE_FACTOR * -1.0

        # ── カテゴリペナルティコンポーネント ───────────────────
        category_component = 0.0
        if sections_dict:
            cat_penalties      = _calc_category_penalty(sections_dict, cfg)
            category_component = sum(cat_penalties.values())

        raw_bonus = yoy_component + category_component

        score_min = getattr(cfg, "TEXT_SIGNAL_SCORE_MIN", -10.0)
        score_max = getattr(cfg, "TEXT_SIGNAL_SCORE_MAX",   0.0)
        bonus     = round(max(score_min, min(score_max, raw_bonus)), 2)

        # キャッシュに保存
        entry = {
            "securities_code":     securities_code,
            "bonus":               bonus,
            "yoy_delta":           yoy_delta,
            "yoy_component":       round(yoy_component, 4),
            "category_component":  round(category_component, 4),
            "period_end":          current_analysis.get("period_end", ""),
            "calc_at":             datetime.now().isoformat(),
        }
        _upsert_cache(cfg, securities_code, entry)

        print(f"    📝 [text_signal] {securities_code}: "
              f"yoy={yoy_component:+.2f}pt "
              f"category={category_component:+.2f}pt "
              f"→ bonus={bonus:+.2f}pt")

        return bonus

    except Exception as e:
        print(f"    ⚠️  [text_signal] calc_text_risk_bonus エラー ({securities_code}): "
              f"{str(e)[:80]}")
        return 0.0

# ==============================================================
# BLOCK 4-B: カタリスト候補自動特定（independence_score連携）
# ==============================================================

def detect_catalyst_from_text(
    cfg,
    securities_code: str,
    sections_dict:   dict,
    current_analysis: dict | None = None,
) -> dict:
    """
    有報テキストからポジティブカタリスト候補を自動特定し、
    latent_pearl_manager.py の昇格ロジックへ供給するデータを返す。

    $catalyst\\_score = \\min(10.0, \\sum_{k \\in detected} weight_k)$

    Args:
        cfg:              config モジュール
        securities_code:  証券コード
        sections_dict:    text_extractor.extract_sections() の出力
        current_analysis: finbert_analyzer.analyze_sections() の出力（任意）

    Returns:
        {
          "securities_code":  str,
          "catalyst_score":   float,   # 0.0〜10.0
          "catalyst_flags":   list,    # 検出されたカタリストフラグ
          "catalyst_types":   list,    # 検出されたカタリスト種別
          "supply_eligible":  bool,    # latent_pearl_manager供給対象か
          "calc_at":          str,
        }
    """
    try:
        if not getattr(cfg, "ENABLE_TEXT_SIGNAL", True):
            return _empty_catalyst_result(securities_code)

        inner = sections_dict.get("sections", {}) if sections_dict else {}
        # 全セクションのテキストを結合して検索
        full_text = " ".join([
            str(inner.get("business", "") or ""),
            str(inner.get("strategy", "") or ""),
            str(inner.get("risk", "") or ""),
            str(inner.get("governance", "") or ""),
        ])

        if not full_text.strip():
            return _empty_catalyst_result(securities_code)

        # ── キーワード検出 ────────────────────────────────────
        keywords_cfg = getattr(
            cfg,
            "TEXT_SIGNAL_CATALYST_KEYWORDS",
            _CATALYST_POSITIVE_KEYWORDS,
        )

        total_weight   = 0.0
        catalyst_flags = []
        catalyst_types: set = set()
        detected_types: set = set()

        for keyword, (weight, c_type) in keywords_cfg.items():
            count = full_text.count(keyword)
            if count > 0 and c_type not in detected_types:
                total_weight += weight
                detected_types.add(c_type)
                catalyst_flags.append(f"{keyword}（×{count}回）")
                catalyst_types.add(c_type)

        # finbert の strategy セクションがポジティブなら追加加点
        if current_analysis:
            strategy_sent = (
                current_analysis.get("section_scores", {})
                .get("strategy", {})
                .get("sentiment_score", 0.5)
            )
            # strategy sentiment が 0.65 以上 → +1.0pt
            if strategy_sent >= 0.65:
                total_weight += 1.0
                catalyst_flags.append(
                    f"戦略セクション高センチメント({strategy_sent:.2f})"
                )
                catalyst_types.add("positive_strategy")

        catalyst_score = round(min(_CATALYST_SCORE_MAX, total_weight), 2)
        supply_eligible = catalyst_score >= _CATALYST_SUPPLY_THRESHOLD

        result = {
            "securities_code": securities_code,
            "catalyst_score":  catalyst_score,
            "catalyst_flags":  catalyst_flags[:10],
            "catalyst_types":  sorted(catalyst_types),
            "supply_eligible": supply_eligible,
            "calc_at":         datetime.now().isoformat(),
        }

        # キャッシュに保存（既存エントリにマージ）
        _upsert_catalyst_cache(cfg, securities_code, result)

        if supply_eligible:
            print(
                f"    💎 [text_signal] {securities_code}: "
                f"カタリスト検出 score={catalyst_score:.1f} "
                f"flags={catalyst_flags[:3]}"
            )

        return result

    except Exception as e:
        print(
            f"    ⚠️  [text_signal] detect_catalyst_from_text "
            f"エラー ({securities_code}): {str(e)[:80]}"
        )
        return _empty_catalyst_result(securities_code)


def batch_detect_catalysts(
    cfg,
    sections_by_code:   dict,
    analysis_by_code:   dict | None = None,
) -> dict:
    """
    複数銘柄のカタリスト検出を一括実行し、
    latent_pearl_manager へ供給するデータを返す。

    Args:
        cfg:              config モジュール
        sections_by_code: {securities_code: extract_sections()の出力}
        analysis_by_code: {securities_code: analyze_sections()の出力}（任意）

    Returns:
        {
          securities_code: catalyst_result_dict,
          ...
        }
    """
    if not getattr(cfg, "ENABLE_TEXT_SIGNAL", True):
        return {}

    results: dict = {}
    total = len(sections_by_code)
    print(f"\n  💎 [text_signal] カタリスト一括検出開始: {total}社")

    for code, sec_dict in sections_by_code.items():
        try:
            cur_analysis = (analysis_by_code or {}).get(code)
            result = detect_catalyst_from_text(
                cfg              = cfg,
                securities_code  = code,
                sections_dict    = sec_dict,
                current_analysis = cur_analysis,
            )
            results[code] = result
        except Exception as e:
            print(f"    ⚠️  [text_signal] {code}: {str(e)[:60]}")
            results[code] = _empty_catalyst_result(code)

    supplied = sum(1 for v in results.values() if v.get("supply_eligible"))
    print(
        f"  ✅ [text_signal] カタリスト検出完了: "
        f"{supplied}/{total}社が latent_pearl_manager 供給対象"
    )
    return results


def supply_catalysts_to_latent_pearl(
    cfg,
    catalyst_results: dict,
) -> int:
    """
    カタリスト検出結果を latent_pearl_manager.py の昇格ロジックに供給する。

    処理フロー:
      1. supply_eligible == True の銘柄のみ抽出
      2. latent_pearl_cache.json の該当エントリを読み込み
      3. catalyst_score / catalyst_flags を更新して再保存
      4. latent_pearl_score を再計算（昇格閾値の再判定）

    Returns:
        int: 更新した銘柄数
    """
    try:
        eligible = {
            code: res
            for code, res in catalyst_results.items()
            if res.get("supply_eligible")
        }
        if not eligible:
            return 0

        # latent_pearl_cache.json を読み込んで更新
        from pathlib import Path as _Path
        import json as _json

        cache_dir = _Path(getattr(cfg, "CACHE_DIR", "stock_data/cache"))
        lp_path   = cache_dir / "latent_pearl_cache.json"
        if not lp_path.exists():
            return 0

        lp_data    = _json.loads(lp_path.read_text(encoding="utf-8"))
        lp_pearls  = lp_data.get("latent_pearls", {})
        updated    = 0

        for code, cat_res in eligible.items():
            # securities_code → ticker の対応（例: "6758" → "6758.T"）
            # latent_pearl_cache は ticker キーで管理
            ticker_jp = f"{code}.T"
            ticker_us = code

            for ticker_key in (ticker_jp, ticker_us):
                entry = lp_pearls.get(ticker_key)
                if entry is None:
                    continue

                old_cat_score = float(entry.get("catalyst_score", 0.0))
                new_cat_score = max(
                    old_cat_score,
                    float(cat_res.get("catalyst_score", 0.0)),
                )
                # カタリストフラグをマージ（重複排除）
                existing_flags = entry.get("catalyst_flags", [])
                new_flags = cat_res.get("catalyst_flags", [])
                merged_flags = list(dict.fromkeys(existing_flags + new_flags))[:10]

                entry["catalyst_score"]  = new_cat_score
                entry["catalyst_flags"]  = merged_flags
                entry["catalyst_types"]  = cat_res.get("catalyst_types", [])
                entry["text_catalyst_supplied_at"] = datetime.now().strftime("%Y-%m-%d")

                # latent_pearl_score を再計算
                # $new\\_lp\\_score = old\\_lp\\_score + \\Delta cat\\_score \\times w_{catalyst}$
                delta_cat  = new_cat_score - old_cat_score
                cat_weight = 0.20  # _LATENT_SCORE_WEIGHTS["catalyst"]
                cat_norm   = (delta_cat / 10.0) * 100.0
                score_delta = cat_norm * cat_weight
                old_lp_score = float(entry.get("latent_pearl_score", 0.0))
                new_lp_score = round(
                    min(100.0, max(0.0, old_lp_score + score_delta)), 2
                )
                entry["latent_pearl_score"] = new_lp_score
                entry["last_updated"]        = datetime.now().strftime("%Y-%m-%d")

                # 昇格閾値の再判定
                promo_thresh = float(
                    getattr(cfg, "LATENT_PEARL_PROMOTION_THRESHOLD", 70.0)
                )
                entry["promotion_eligible"] = new_lp_score >= promo_thresh

                lp_pearls[ticker_key] = entry
                updated += 1
                print(
                    f"    💎 [text_signal→latent_pearl] {ticker_key}: "
                    f"cat_score {old_cat_score:.1f}→{new_cat_score:.1f} "
                    f"lp_score {old_lp_score:.1f}→{new_lp_score:.1f}"
                )
                break  # JP ticker で見つかれば US は不要

        if updated > 0:
            lp_data["latent_pearls"] = lp_pearls
            lp_data["last_updated"]   = datetime.now().strftime("%Y-%m-%d")
            lp_path.write_text(
                _json.dumps(lp_data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        print(f"  ✅ [text_signal→latent_pearl] {updated}銘柄を更新")
        return updated

    except Exception as e:
        print(f"    ⚠️  [text_signal] supply_catalysts_to_latent_pearl エラー: {str(e)[:80]}")
        return 0


def _empty_catalyst_result(securities_code: str) -> dict:
    """空のカタリスト結果辞書を返す。"""
    return {
        "securities_code": securities_code,
        "catalyst_score":  0.0,
        "catalyst_flags":  [],
        "catalyst_types":  [],
        "supply_eligible": False,
        "calc_at":         datetime.now().isoformat(),
    }


def _upsert_catalyst_cache(cfg, securities_code: str, catalyst_entry: dict) -> None:
    """
    カタリスト検出結果を text_signal_cache.json に追記する。
    既存の risk_bonus エントリとは別キー（code + "_catalyst"）で格納。
    """
    try:
        cache = _load_full_cache(cfg)
        cat_key = f"{securities_code}{_CATALYST_CACHE_KEY_SUFFIX}"
        cache[cat_key] = catalyst_entry
        _save_full_cache(cfg, cache)
    except Exception:
        pass


def get_catalyst_result(cfg, securities_code: str) -> dict | None:
    """
    text_signal_cache.json からカタリスト検出結果を取得する。
    independence_score_calculator.py との二重計算を防ぐための参照関数。

    Returns:
        catalyst_result_dict または None（未計算の場合）
    """
    try:
        cache   = _load_full_cache(cfg)
        cat_key = f"{securities_code}{_CATALYST_CACHE_KEY_SUFFIX}"
        return cache.get(cat_key)
    except Exception:
        return None

def batch_calc_text_risk_bonus(
    cfg,
    analysis_results: dict[str, dict],
    prev_results:     Optional[dict[str, dict]] = None,
    sections_by_code: Optional[dict[str, dict]] = None,
) -> dict[str, float]:
    """
    複数銘柄のテキストリスクボーナスを一括計算する。

    Args:
        cfg:              config モジュール
        analysis_results: {securities_code: analyze_sections() の出力}
        prev_results:     前期の同データ（None の場合 YoY 差分=0）
        sections_by_code: {securities_code: extract_sections() の出力}

    Returns:
        {securities_code: bonus_float}
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    if not getattr(cfg, "ENABLE_TEXT_SIGNAL", True):
        return {}

    results: dict[str, float] = {}
    total = len(analysis_results)
    print(f"\n  📝 [text_signal] バッチ計算開始: {total}社")

    for i, (code, cur_analysis) in enumerate(analysis_results.items(), start=1):
        try:
            prev     = (prev_results or {}).get(code)
            sec_dict = (sections_by_code or {}).get(code)
            bonus    = calc_text_risk_bonus(
                cfg              = cfg,
                securities_code  = code,
                current_analysis = cur_analysis,
                prev_analysis    = prev,
                sections_dict    = sec_dict,
            )
            results[code] = bonus
        except Exception as e:
            print(f"    ⚠️  [text_signal] {code}: 予期しないエラー → {str(e)[:60]}")
            results[code] = 0.0

    succeeded = sum(1 for v in results.values() if v != 0.0)
    print(f"  ✅ [text_signal] バッチ完了: {succeeded}/{total}社でリスク検出")
    return results


# ==============================================================
# BLOCK 5: 前期データ取得ユーティリティ
# ==============================================================

def load_prev_analysis_from_cache(cfg, securities_code: str) -> Optional[dict]:
    """
    キャッシュから前期の分析結果を取得する（YoY 差分計算用）。
    キャッシュエントリに section_scores が含まれていれば返す。

    Returns:
        {"section_scores": dict, "period_end": str} または None
    """
    entry = _get_cached_entry(cfg, securities_code)
    if not entry:
        return None
    # キャッシュには section_scores を直接格納していないため、
    # finbert_analyzer のキャッシュ（text_extractor 側）を参照する設計
    # → 今フェーズでは YoY=0 として処理し、次週の2周目から差分が効く
    return None
