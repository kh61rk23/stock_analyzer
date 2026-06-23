# ==============================================================
# geo_signal.py  v1.0  (Wave 6 - G-X1)
# ==============================================================
# 【役割】
#   地政学リスク × 企業権益エクスポージャーを統合し、
#   個別銘柄の geo_impact_bonus（composite_score への加算点）を
#   計算するコアエンジン。JP株・US株の両方に対応する。
#
# 【計算フロー】
#
#   Step 1: 権益エクスポージャー取得
#     BQ geopolitics.company_country_exposure から
#     ticker の有効レコードを取得する。
#     BQ 未接続時は jogmec_scraper / sec_geographic_extractor の
#     ローカルマスターをフォールバックとして使用する。
#
#   Step 2: 国別リスクスコア取得
#     BQ geopolitics.country_risk_daily から
#     各権益国の composite_risk_score を取得する。
#
#   Step 3: 生インパクト計算
#     各権益エントリについて:
#       $raw\_impact_i = exposure\_strength_i \times risk\_score_i \times scale$
#     方向性係数（config.GEO_COMMODITY_DIRECTION_JP/US）を適用:
#       $directed\_impact_i = raw\_impact_i \times direction\_coeff_i$
#     全エントリを合算してクリップ:
#       $geo\_raw = \sum_i directed\_impact_i$
#       $geo\_impact\_bonus = \text{clip}(geo\_raw, GEO\_IMPACT\_MIN, GEO\_IMPACT\_MAX)$
#
#   Step 4: Phase T クラスター伝播（taxonomy 連鎖）
#     config.GEO_TAXONOMY_PROPAGATION を参照し、
#     権益資源に連鎖する技術ノードへのスコア伝播を計算する。
#     伝播強度は 0.3 に減衰して加算する（過補正防止）。
#       $propagation\_bonus = \sum_{node \in propagation(commodity)} raw\_impact \times 0.3$
#
#   Step 5: 最終スコア
#     $geo\_impact\_bonus = \text{clip}(
#       geo\_raw + propagation\_bonus,
#       GEO\_IMPACT\_MIN,
#       GEO\_IMPACT\_MAX
#     )$
#
# 【数式サマリー】
#   $raw\_impact_i = exposure\_strength_i \times (risk\_score_i / 100) \times GEO\_IMPACT\_SCALE$
#   $directed\_impact_i = raw\_impact_i \times direction\_coeff_i$
#   $geo\_impact\_bonus = \text{clip}(\sum_i directed\_impact_i, MIN, MAX)$
#
# 【呼び出し元】
#   base_agent.py:      calc_geo_impact_bonus_jp(ticker, sector_code, cfg)
#   us_stock_screener.py: calc_geo_impact_bonus_us(ticker, gics, cfg)
#   signal_aggregator.py: aggregate_geo_signal(results, code, cfg)
#
# 【依存】
#   edinet_bq_client.py / config.py
#   jogmec_scraper.py（フォールバック参照）
#   country_risk_updater.py（リスクスコア取得）
# ==============================================================

from __future__ import annotations

import time
from datetime import datetime, timezone, date
from typing import Any

# ==============================================================
# BLOCK 1: BQ アクセスユーティリティ（キャッシュ付き）
# ==============================================================

# セッション内キャッシュ（BQ 接続コストを削減）
_RAW_BQ_CACHE: Any = None
_RISK_SCORE_CACHE: dict[str, float] = {}     # {country_code: score}
_EXPOSURE_CACHE:   dict[str, list]  = {}     # {ticker: exposure_list}
_CACHE_DATE: str = ""                         # キャッシュの日付（日次リセット）


def _get_raw_bq(project_id: str = "stock-analyzer-edinet"):
    """BQ Client を返す（セッション内キャッシュ）。失敗時 None。"""
    global _RAW_BQ_CACHE
    if _RAW_BQ_CACHE is not None:
        return _RAW_BQ_CACHE
    try:
        import importlib, sys as _sys
        from pathlib import Path as _Path
        for p in ["/content", str(_Path(__file__).parent)]:
            if p not in _sys.path:
                _sys.path.insert(0, p)
        _bq_mod = importlib.import_module("edinet_bq_client")
        _RAW_BQ_CACHE = _bq_mod.EDINETBQClient().client
        return _RAW_BQ_CACHE
    except Exception:
        return None


def _reset_daily_cache() -> None:
    """日付が変わったらキャッシュをリセットする。"""
    global _RISK_SCORE_CACHE, _EXPOSURE_CACHE, _CACHE_DATE
    today = date.today().isoformat()
    if _CACHE_DATE != today:
        _RISK_SCORE_CACHE = {}
        _EXPOSURE_CACHE   = {}
        _CACHE_DATE       = today


# ==============================================================
# BLOCK 2: 権益エクスポージャー取得
# ==============================================================

def _get_exposure_list(
    ticker: str,
    market: str,
    cfg=None,
    project_id: str = "stock-analyzer-edinet",
    prefetched: list | None = None,
) -> list[dict]:
    """
    ティッカーの権益エクスポージャーリストを返す。

    取得優先順:
      1. prefetched（us_stock_screener.py が事前取得した c["geo_exposures"]）
      2. セッションキャッシュ
      3. BQ geopolitics.company_country_exposure
      4. jogmec_scraper の手動マスター（JP株のみ・BQ 未接続フォールバック）

    Returns:
        list of {country_code, exposure_type, exposure_strength,
                 commodity_code, composite_risk_score}
    """
    _reset_daily_cache()
    _pid = getattr(cfg, "GEO_BQ_PROJECT_ID", project_id) if cfg else project_id

    # ① 事前取得データ
    if prefetched is not None:
        return prefetched

    # ② キャッシュ
    cache_key = f"{ticker}_{market}"
    if cache_key in _EXPOSURE_CACHE:
        return _EXPOSURE_CACHE[cache_key]

    # ③ BQ
    raw_bq = _get_raw_bq(_pid)
    if raw_bq:
        try:
            query = f"""
            SELECT
              e.country_code,
              e.exposure_type,
              e.exposure_strength,
              e.commodity_code,
              IFNULL(r.composite_risk_score, 0.0) AS composite_risk_score
            FROM `{_pid}.geopolitics.company_country_exposure` AS e
            LEFT JOIN (
              SELECT country_code, composite_risk_score
              FROM (
                SELECT country_code, composite_risk_score,
                       ROW_NUMBER() OVER (
                         PARTITION BY country_code
                         ORDER BY risk_date DESC
                       ) AS rn
                FROM `{_pid}.geopolitics.country_risk_daily`
              )
              WHERE rn = 1
            ) AS r ON e.country_code = r.country_code
            WHERE e.ticker = '{ticker}'
              AND e.market  = '{market}'
              AND (e.valid_to IS NULL OR e.valid_to > CURRENT_DATE())
            ORDER BY e.exposure_strength DESC
            """
            rows = list(raw_bq.query(query).result())
            result = [
                {
                    "country_code":         row.country_code,
                    "exposure_type":        row.exposure_type,
                    "exposure_strength":    float(row.exposure_strength or 0.0),
                    "commodity_code":       row.commodity_code,
                    "composite_risk_score": float(
                        row.composite_risk_score or 0.0
                    ),
                }
                for row in rows
            ]
            _EXPOSURE_CACHE[cache_key] = result
            return result
        except Exception as e:
            print(f"  ⚠️  geo_signal exposure BQ [{ticker}]: {str(e)[:60]}")

    # ④ フォールバック: JP株のみ jogmec_scraper 手動マスターを参照
    if market == "JP":
        try:
            import importlib, sys as _sys
            from pathlib import Path as _Path
            for p in ["/content", str(_Path(__file__).parent)]:
                if p not in _sys.path:
                    _sys.path.insert(0, p)
            _js = importlib.import_module("jogmec_scraper")
            manual = [
                r for r in _js.MANUAL_EXPOSURE_MASTER
                if r.get("ticker") == ticker
            ]
            result = [
                {
                    "country_code":         r["country_code"],
                    "exposure_type":        r["exposure_type"],
                    "exposure_strength":    float(r.get("exposure_strength", 0.1)),
                    "commodity_code":       r.get("commodity_code"),
                    "composite_risk_score": 0.0,   # リスクスコア未取得
                }
                for r in manual
            ]
            _EXPOSURE_CACHE[cache_key] = result
            return result
        except Exception:
            pass

    _EXPOSURE_CACHE[cache_key] = []
    return []


# ==============================================================
# BLOCK 3: 国別リスクスコア取得（バッチ）
# ==============================================================

def _get_risk_scores_for_countries(
    country_codes: list[str],
    cfg=None,
    project_id: str = "stock-analyzer-edinet",
) -> dict[str, float]:
    """
    複数国のリスクスコアを一括取得する（キャッシュ優先）。

    Returns:
        {country_code: composite_risk_score}
    """
    _reset_daily_cache()
    _pid = getattr(cfg, "GEO_BQ_PROJECT_ID", project_id) if cfg else project_id

    result: dict[str, float] = {}
    missing: list[str] = []

    for code in country_codes:
        if code in _RISK_SCORE_CACHE:
            result[code] = _RISK_SCORE_CACHE[code]
        else:
            missing.append(code)

    if not missing:
        return result

    # BQ から一括取得
    raw_bq = _get_raw_bq(_pid)
    if raw_bq:
        try:
            codes_str = ", ".join(f"'{c}'" for c in missing)
            query = f"""
            SELECT country_code, composite_risk_score
            FROM (
              SELECT country_code, composite_risk_score,
                     ROW_NUMBER() OVER (
                       PARTITION BY country_code
                       ORDER BY risk_date DESC
                     ) AS rn
              FROM `{_pid}.geopolitics.country_risk_daily`
              WHERE country_code IN ({codes_str})
            )
            WHERE rn = 1
            """
            for row in raw_bq.query(query).result():
                score = float(row.composite_risk_score or 0.0)
                result[row.country_code]              = score
                _RISK_SCORE_CACHE[row.country_code]   = score
        except Exception as e:
            print(f"  ⚠️  geo_signal risk_scores BQ: {str(e)[:60]}")

    # BQ 未取得分はフォールバック（country_risk_updater のローカル辞書）
    for code in missing:
        if code not in result:
            try:
                import importlib, sys as _sys
                from pathlib import Path as _Path
                for p in ["/content", str(_Path(__file__).parent)]:
                    if p not in _sys.path:
                        _sys.path.insert(0, p)
                _cru = importlib.import_module("country_risk_updater")
                data = _cru.get_latest_risk_score(
                    code, cfg=cfg, project_id=_pid
                )
                score = float(data.get("composite_risk_score", 0.0))
                result[code]             = score
                _RISK_SCORE_CACHE[code]  = score
            except Exception:
                result[code]            = 0.0
                _RISK_SCORE_CACHE[code] = 0.0

    return result


# ==============================================================
# BLOCK 4: 方向性係数取得
# ==============================================================

def _get_direction_coeff(
    commodity_code: str | None,
    sector_or_gics: str,
    market: str,
    cfg=None,
) -> float:
    """
    資源コード × セクターコード の方向性係数を返す。

    JP株:  config.GEO_COMMODITY_DIRECTION_JP[commodity][sector_code]
    US株:  config.GEO_COMMODITY_DIRECTION_US[commodity][gics_etf]

    方向性係数の意味:
      +1.0: その資源の価格上昇/供給不安がそのセクターに追い風
      -1.0: 逆風
       0.0: 影響なし

    Returns:
        float: 方向性係数（未定義時は 0.0）
    """
    if not commodity_code or not cfg:
        return 0.0

    try:
        if market == "JP":
            direction_table = getattr(
                cfg, "GEO_COMMODITY_DIRECTION_JP", {}
            )
        else:
            direction_table = getattr(
                cfg, "GEO_COMMODITY_DIRECTION_US", {}
            )

        commodity_map = direction_table.get(commodity_code, {})
        if not commodity_map:
            return 0.0

        # セクターコードに直接一致するものを優先
        if sector_or_gics in commodity_map:
            return float(commodity_map[sector_or_gics])

        # 部分一致（US株の ETF シンボル等）
        for key, val in commodity_map.items():
            if key in sector_or_gics or sector_or_gics in key:
                return float(val)

        return 0.0

    except Exception:
        return 0.0


# ==============================================================
# BLOCK 5: Phase T クラスター伝播計算
# ==============================================================

def _calc_taxonomy_propagation(
    exposures: list[dict],
    sector_or_gics: str,
    market: str,
    cfg=None,
) -> float:
    """
    Phase T クラスター伝播ロジック。
    権益資源コードに連鎖する技術ノードへのスコア伝播を計算する。

    伝播ルール（config.GEO_TAXONOMY_PROPAGATION）:
      例: lithium → ["ev_battery", "semiconductor_logic", "automotive"]
      伝播強度: raw_impact × 0.3（減衰係数）

    伝播方向は技術ノードの「当該セクターとの関連性」で決まる。
    関連性が不明な場合は 0 として扱う（過補正防止）。

    Returns:
        float: 伝播ボーナス（$-GEO\\_IMPACT\\_MAX \times 0.5$ 〜 $+GEO\\_IMPACT\\_MAX \times 0.5$）
    """
    if not cfg:
        return 0.0

    propagation_map = getattr(cfg, "GEO_TAXONOMY_PROPAGATION", {})
    if not propagation_map:
        return 0.0

    _scale     = float(getattr(cfg, "GEO_IMPACT_SCALE", 0.30))
    _decay     = 0.3   # 伝播減衰係数（直接効果の30%）
    _impact_max = float(getattr(cfg, "GEO_IMPACT_MAX", 15.0))

    total_propagation = 0.0

    for exp in exposures:
        commodity = exp.get("commodity_code")
        if not commodity:
            continue

        nodes = propagation_map.get(commodity, [])
        if not nodes:
            continue

        strength   = float(exp.get("exposure_strength", 0.0))
        risk_score = float(exp.get("composite_risk_score", 0.0))
        raw_impact = strength * (risk_score / 100.0) * _scale

        # 各連鎖ノードへの伝播
        for node in nodes:
            # セクターとノードの関連性を簡易判定
            # （完全実装は taxonomy.company_taxonomy_map JOIN だが
            #   Phase G では近似として secotr_or_gics のキーワード一致を使用）
            relevance = _estimate_node_relevance(
                node, sector_or_gics, market
            )
            propagation = raw_impact * _decay * relevance
            total_propagation += propagation

    # 伝播ボーナスを GEO_IMPACT_MAX の50%に制限
    max_prop = _impact_max * 0.5
    return round(max(-max_prop, min(max_prop, total_propagation)), 4)


def _estimate_node_relevance(
    node_id: str,
    sector_or_gics: str,
    market: str,
) -> float:
    """
    技術ノードとセクターコードの関連性を簡易推定する（0〜1）。

    完全実装では taxonomy.company_taxonomy_map を参照するが、
    Phase G では以下のキーワードマッピングで近似する。
    """
    # JP 業種コード × ノードID の関連性テーブル
    _JP_NODE_SECTOR_RELEVANCE: dict[str, list[str]] = {
        "ev_battery":         ["1016", "1017", "1013"],
        "semiconductor_logic": ["1016", "1025"],
        "automotive":          ["1017", "1015"],
        "connector":           ["1016", "1014"],
        "sensor":              ["1016", "1018"],
        "petrochemical":       ["1007", "1009"],
        "synthetic_fiber":     ["1007", "1005"],
        "plastic":             ["1007"],
        "wind_energy":         ["1020", "1016"],
        "ev_motor":            ["1016", "1017"],
        "stainless_steel":     ["1012", "1013"],
        "aerospace":           ["1016", "1017"],
    }
    # US GICS ETF × ノードID の関連性テーブル
    _US_NODE_GICS_RELEVANCE: dict[str, list[str]] = {
        "ev_battery":          ["XLK", "XLY", "XLB"],
        "semiconductor_logic": ["XLK"],
        "automotive":          ["XLY"],
        "connector":           ["XLK", "XLI"],
        "sensor":              ["XLK"],
        "petrochemical":       ["XLB", "XLE"],
        "synthetic_fiber":     ["XLB"],
        "plastic":             ["XLB"],
        "wind_energy":         ["XLU", "XLI"],
        "ev_motor":            ["XLK", "XLY"],
        "stainless_steel":     ["XLB"],
        "aerospace":           ["XLI"],
    }

    if market == "JP":
        related = _JP_NODE_SECTOR_RELEVANCE.get(node_id, [])
    else:
        related = _US_NODE_GICS_RELEVANCE.get(node_id, [])

    if sector_or_gics in related:
        return 1.0
    # 部分一致（ETFシンボルが sector 文字列に含まれる等）
    for r in related:
        if r in sector_or_gics or sector_or_gics in r:
            return 0.5
    return 0.0


# ==============================================================
# BLOCK 6: JP株用 geo_impact_bonus 計算
# ==============================================================

def calc_geo_impact_bonus_jp(
    ticker:      str,
    sector_code: str,
    cfg=None,
    project_id:  str = "stock-analyzer-edinet",
    prefetched_exposures: list | None = None,
) -> float:
    """
    JP株の geo_impact_bonus を計算して返す。
    base_agent.py の build_result() から呼び出される。

    計算式:
      $geo\\_impact\\_bonus = \text{clip}(
        \\sum_i directed\\_impact_i + propagation\\_bonus,
        GEO\\_IMPACT\\_MIN, GEO\\_IMPACT\\_MAX
      )$

    Args:
        ticker       : JP株ティッカー（例: "7203.T"）
        sector_code  : 東証33業種コード（例: "1017"）
        cfg          : config モジュール
        project_id   : GCPプロジェクトID
        prefetched_exposures: 事前取得済み exposure リスト（省略可）

    Returns:
        float: geo_impact_bonus（$[GEO\\_IMPACT\\_MIN, GEO\\_IMPACT\\_MAX]$）
    """
    if not getattr(cfg, "ENABLE_GEO_SIGNAL", False):
        return 0.0

    try:
        _pid        = getattr(cfg, "GEO_BQ_PROJECT_ID", project_id) if cfg else project_id
        _scale      = float(getattr(cfg, "GEO_IMPACT_SCALE",  0.30))
        _impact_max = float(getattr(cfg, "GEO_IMPACT_MAX",   15.0))
        _impact_min = float(getattr(cfg, "GEO_IMPACT_MIN",  -15.0))

        # ① 権益エクスポージャー取得
        exposures = _get_exposure_list(
            ticker, "JP", cfg, _pid,
            prefetched=prefetched_exposures,
        )
        if not exposures:
            return 0.0

        # ② 国別リスクスコア一括取得
        country_codes = list({e["country_code"] for e in exposures})
        risk_scores = _get_risk_scores_for_countries(
            country_codes, cfg, _pid
        )

        # ③ 生インパクト計算
        geo_raw = 0.0
        for exp in exposures:
            cc       = exp["country_code"]
            strength = float(exp.get("exposure_strength", 0.0))
            risk     = risk_scores.get(cc, float(
                exp.get("composite_risk_score", 0.0)
            ))
            commodity = exp.get("commodity_code")

            raw_impact = strength * (risk / 100.0) * _scale

            # 方向性係数を適用
            direction = _get_direction_coeff(
                commodity, sector_code, "JP", cfg
            )
            if direction == 0.0 and raw_impact > 0:
                # 方向性未定義の場合はリスク高国への逆風として扱う
                direction = -0.5 if risk >= 50.0 else 0.0

            geo_raw += raw_impact * direction

        # ④ Phase T クラスター伝播
        propagation = _calc_taxonomy_propagation(
            exposures, sector_code, "JP", cfg
        )

        # ⑤ 最終スコア
        total = geo_raw + propagation
        bonus = round(max(_impact_min, min(_impact_max, total)), 4)

        if abs(bonus) >= 1.0:
            print(
                f"    🌐 [G-X] geo_impact [{ticker}/{sector_code}]: "
                f"raw={geo_raw:+.2f} prop={propagation:+.2f} "
                f"→ bonus={bonus:+.2f}pt "
                f"({len(exposures)}権益)"
            )

        return bonus

    except Exception as e:
        print(f"  ⚠️  calc_geo_impact_bonus_jp [{ticker}]: {str(e)[:80]}")
        return 0.0


# ==============================================================
# BLOCK 7: US株用 geo_impact_bonus 計算
# ==============================================================

def calc_geo_impact_bonus_us(
    ticker:      str,
    gics_sector: str,
    cfg=None,
    project_id:  str = "stock-analyzer-edinet",
    prefetched_exposures: list | None = None,
) -> float:
    """
    US株の geo_impact_bonus を計算して返す。
    us_stock_screener.py の run_us_stock_screener() から呼び出される。

    Args:
        ticker       : 米国株ティッカー（例: "NVDA"）
        gics_sector  : GICSセクターETFシンボル（例: "XLK"）
                       または yfinance sector 文字列（例: "Technology"）
        cfg          : config モジュール
        project_id   : GCPプロジェクトID
        prefetched_exposures: 事前取得済み exposure リスト（省略可）

    Returns:
        float: geo_impact_bonus（$[GEO\\_IMPACT\\_MIN, GEO\\_IMPACT\\_MAX]$）
    """
    if not getattr(cfg, "ENABLE_GEO_SIGNAL", False):
        return 0.0

    try:
        _pid        = getattr(cfg, "GEO_BQ_PROJECT_ID", project_id) if cfg else project_id
        _scale      = float(getattr(cfg, "GEO_IMPACT_SCALE",  0.30))
        _impact_max = float(getattr(cfg, "GEO_IMPACT_MAX",   15.0))
        _impact_min = float(getattr(cfg, "GEO_IMPACT_MIN",  -15.0))

        # yfinance sector 文字列 → ETF シンボルに変換
        _GICS_ETF_MAP = {
            "Technology":             "XLK",
            "Consumer Cyclical":      "XLY",
            "Consumer Defensive":     "XLP",
            "Financial Services":     "XLF",
            "Healthcare":             "XLV",
            "Energy":                 "XLE",
            "Basic Materials":        "XLB",
            "Industrials":            "XLI",
            "Communication Services": "XLC",
            "Utilities":              "XLU",
            "Real Estate":            "XLRE",
            "Financial":              "XLF",
            "Health Care":            "XLV",
            "Consumer Staples":       "XLP",
            "Information Technology": "XLK",
        }
        gics_key = _GICS_ETF_MAP.get(gics_sector, gics_sector)

        # ① 権益エクスポージャー取得
        exposures = _get_exposure_list(
            ticker, "US", cfg, _pid,
            prefetched=prefetched_exposures,
        )
        if not exposures:
            return 0.0

        # ② 国別リスクスコア一括取得
        country_codes = list({e["country_code"] for e in exposures})
        risk_scores = _get_risk_scores_for_countries(
            country_codes, cfg, _pid
        )

        # ③ 生インパクト計算
        geo_raw = 0.0
        for exp in exposures:
            cc       = exp["country_code"]
            strength = float(exp.get("exposure_strength", 0.0))
            risk     = risk_scores.get(cc, float(
                exp.get("composite_risk_score", 0.0)
            ))
            commodity = exp.get("commodity_code")

            raw_impact = strength * (risk / 100.0) * _scale

            direction = _get_direction_coeff(
                commodity, gics_key, "US", cfg
            )
            if direction == 0.0 and raw_impact > 0:
                direction = -0.5 if risk >= 50.0 else 0.0

            geo_raw += raw_impact * direction

        # ④ Phase T クラスター伝播
        propagation = _calc_taxonomy_propagation(
            exposures, gics_key, "US", cfg
        )

        # ⑤ 最終スコア
        total = geo_raw + propagation
        bonus = round(max(_impact_min, min(_impact_max, total)), 4)

        if abs(bonus) >= 1.0:
            print(
                f"    🌐 [G-X] geo_impact_us [{ticker}/{gics_key}]: "
                f"raw={geo_raw:+.2f} prop={propagation:+.2f} "
                f"→ bonus={bonus:+.2f}pt "
                f"({len(exposures)}権益)"
            )

        return bonus

    except Exception as e:
        print(f"  ⚠️  calc_geo_impact_bonus_us [{ticker}]: {str(e)[:80]}")
        return 0.0


# ==============================================================
# BLOCK 8: signal_aggregator 向け集計関数
# ==============================================================

def aggregate_geo_signal(
    sector_code: str,
    results:     dict,
    cfg=None,
    market:      str = "JP",
) -> float:
    """
    signal_aggregator.py の calc_signal_score() から呼ばれる。
    業種全体の geo_impact_bonus 平均を 0〜100 スコアに変換して返す。

    変換式:
      $signal\\_score = 50 + avg\\_bonus \times (50 / GEO\\_IMPACT\\_MAX)$
      （bonus が MAX なら 100, MIN なら 0, 0 なら 50）

    Args:
        sector_code : JP 業種コード（例: "1016"）または US GICS ETF（"XLK"）
        results     : run_all_tasks() の results dict
        cfg         : config モジュール
        market      : "JP" / "US"

    Returns:
        float: 0〜100 のシグナルスコア（データなし時は None）
    """
    if not getattr(cfg, "ENABLE_GEO_SIGNAL", False):
        return None

    try:
        _impact_max = float(getattr(cfg, "GEO_IMPACT_MAX", 15.0))
        _impact_min = float(getattr(cfg, "GEO_IMPACT_MIN", -15.0))

        # screener 通過銘柄から geo_impact_bonus を収集
        if market == "JP":
            sector_results = results.get("sectors", {})
            sector_data    = sector_results.get(str(sector_code), {})
            companies      = sector_data.get("top_companies", [])
            bonuses = [
                float(c.get("geo_impact_bonus", 0.0))
                for c in companies
                if c.get("fetch_ok") and c.get("geo_impact_bonus") is not None
            ]
        else:
            screener = results.get("us_screener", {})
            passed   = screener.get("passed", [])
            bonuses  = [
                float(c.get("geo_impact_bonus", 0.0))
                for c in passed
                if str(c.get("gics_sector", c.get("sector", "")))
                   in (sector_code,)
                and c.get("geo_impact_bonus") is not None
            ]

        if not bonuses:
            return None

        avg_bonus = sum(bonuses) / len(bonuses)
        # $score = 50 + avg\_bonus \times (50 / GEO\_IMPACT\_MAX)$
        scale = 50.0 / max(abs(_impact_max), abs(_impact_min))
        score = 50.0 + avg_bonus * scale
        return round(max(0.0, min(100.0, score)), 1)

    except Exception as e:
        print(f"  ⚠️  aggregate_geo_signal [{sector_code}]: {str(e)[:60]}")
        return None


# ==============================================================
# BLOCK 9: 週次レポート向けサマリー生成
# ==============================================================

def build_geo_weekly_summary(
    results:  dict,
    cfg=None,
    top_n:    int = 5,
) -> dict:
    """
    weekly_report_generator.py から呼ばれる。
    地政学リスクと権益影響のサマリーを返す。

    Returns:
        {
          "high_risk_countries":  list[dict],  # {country_code, score, affected_tickers}
          "top_positive_tickers": list[dict],  # {ticker, bonus}
          "top_negative_tickers": list[dict],  # {ticker, bonus}
          "summary_text":         str,
        }
    """
    if not getattr(cfg, "ENABLE_GEO_SIGNAL", False):
        return {
            "high_risk_countries":  [],
            "top_positive_tickers": [],
            "top_negative_tickers": [],
            "summary_text":         "地政学シグナル: 無効（ENABLE_GEO_SIGNAL=False）",
        }

    try:
        geo_risk = results.get("geo_risk", {})
        risk_scores  = geo_risk.get("risk_scores", {})
        high_risk_codes = geo_risk.get("high_risk", [])

        # 高リスク国の影響銘柄を集計
        _pid = getattr(cfg, "GEO_BQ_PROJECT_ID",
                       "stock-analyzer-edinet") if cfg else "stock-analyzer-edinet"
        high_risk_details = []
        for code in high_risk_codes[:top_n]:
            score = risk_scores.get(code, 0.0)
            # その国に権益を持つティッカーを BQ から取得
            affected: list[str] = []
            try:
                raw_bq = _get_raw_bq(_pid)
                if raw_bq:
                    q = f"""
                    SELECT DISTINCT ticker
                    FROM `{_pid}.geopolitics.company_country_exposure`
                    WHERE country_code = '{code}'
                      AND (valid_to IS NULL OR valid_to > CURRENT_DATE())
                    LIMIT 5
                    """
                    affected = [
                        row.ticker
                        for row in raw_bq.query(q).result()
                    ]
            except Exception:
                pass
            high_risk_details.append({
                "country_code":     code,
                "risk_score":       round(score, 1),
                "affected_tickers": affected,
            })

        # geo_impact_bonus の上位・下位銘柄を集計（screener 結果から）
        all_bonuses: list[dict] = []

        # JP 株
        for sc_code, sc_data in results.get("sectors", {}).items():
            if not isinstance(sc_data, dict):
                continue
            for c in sc_data.get("top_companies", []):
                bonus = c.get("geo_impact_bonus")
                if bonus is not None and abs(float(bonus)) >= 1.0:
                    all_bonuses.append({
                        "ticker": c.get("ticker", ""),
                        "bonus":  float(bonus),
                        "market": "JP",
                    })

        # US 株
        for c in results.get("us_screener", {}).get("passed", []):
            bonus = c.get("geo_impact_bonus")
            if bonus is not None and abs(float(bonus)) >= 1.0:
                all_bonuses.append({
                    "ticker": c.get("ticker", ""),
                    "bonus":  float(bonus),
                    "market": "US",
                })

        all_bonuses.sort(key=lambda x: -x["bonus"])
        top_pos = all_bonuses[:top_n]
        top_neg = sorted(all_bonuses, key=lambda x: x["bonus"])[:top_n]

        # サマリーテキスト生成
        lines = ["### 🌐 地政学リスク・資源権益サマリー", ""]
        if high_risk_details:
            lines.append("**高リスク国（composite_risk_score ≥ 50）:**")
            for d in high_risk_details:
                tickers_str = (
                    "、".join(d["affected_tickers"])
                    if d["affected_tickers"]
                    else "対象銘柄なし"
                )
                lines.append(
                    f"- `{d['country_code']}` スコア {d['risk_score']:.0f}/100 "
                    f"→ 影響銘柄: {tickers_str}"
                )
        else:
            lines.append("*高リスク国の検出なし*")

        lines.append("")
        if top_pos:
            lines.append("**地政学追い風銘柄 TOP:**")
            for d in top_pos:
                lines.append(
                    f"- `{d['ticker']}` ({d['market']}) "
                    f"geo_impact_bonus: {d['bonus']:+.2f}pt"
                )
        if top_neg:
            lines.append("**地政学逆風銘柄 TOP:**")
            for d in top_neg:
                lines.append(
                    f"- `{d['ticker']}` ({d['market']}) "
                    f"geo_impact_bonus: {d['bonus']:+.2f}pt"
                )

        return {
            "high_risk_countries":  high_risk_details,
            "top_positive_tickers": top_pos,
            "top_negative_tickers": top_neg,
            "summary_text":         "\n".join(lines),
        }

    except Exception as e:
        print(f"  ⚠️  build_geo_weekly_summary: {str(e)[:80]}")
        return {
            "high_risk_countries":  [],
            "top_positive_tickers": [],
            "top_negative_tickers": [],
            "summary_text":         f"地政学サマリー生成エラー: {str(e)[:60]}",
        }


# ==============================================================
# 単体テスト / Colab実行エントリ
# ==============================================================

if __name__ == "__main__":
    import sys

    print("=" * 60)
    print("geo_signal.py v1.0")
    print("=" * 60)

    # テスト: JP株 INPEX（石油権益最多）
    print("\n--- JP株テスト: 1605.T (INPEX) 業種=1009 ---")
    bonus_jp = calc_geo_impact_bonus_jp(
        ticker      = "1605.T",
        sector_code = "1009",
        cfg         = None,
        project_id  = "stock-analyzer-edinet",
    )
    print(f"geo_impact_bonus (JP): {bonus_jp:+.4f}")

    # テスト: US株 XOM（エネルギー権益）
    print("\n--- US株テスト: XOM (Exxon) GICS=XLE ---")
    bonus_us = calc_geo_impact_bonus_us(
        ticker      = "XOM",
        gics_sector = "Energy",
        cfg         = None,
        project_id  = "stock-analyzer-edinet",
    )
    print(f"geo_impact_bonus (US): {bonus_us:+.4f}")

    print("\n✅ geo_signal.py 単体テスト完了")