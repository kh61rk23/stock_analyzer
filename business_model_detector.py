# ==============================================================
# business_model_detector.py  v1.0  (BM Ph1)
# 業態自動判定モジュール
# ==============================================================
# 【役割】
#   業種コード・財務比率・売上高から業態（BM-01〜BM-12）を自動判定する。
#   size_tier_classifier.py から呼ばれる共通ライブラリ。
#
# 【判定ロジック（優先順位順）】
#   Priority 1: 財務比率オーバーライドルール（データがある場合のみ）
#     - gross_margin >= 0.60 AND (revenue_per_employee >= 2000万 OR
#       recurring_revenue_ratio >= 0.70) → BM-04（SaaS）
#     - debt_ratio >= 0.80 → BM-06（金融）
#     - asset_turnover <= 0.50 AND gross_margin < 0.40 → BM-01（装置産業）
#     - recurring_revenue_ratio >= 0.70 AND gross_margin < 0.60 → BM-05（PF）
#   Priority 2: BM_SECTOR_MAP（業種コードからの一次マッピング）
#   Priority 3: デフォルト（BM-10 サービス）
#
# 【出力スキーマ】
#   {
#     "bm_type":         str,    # "BM-04"
#     "bm_label":        str,    # "ソフトウェア・SaaS"
#     "bm_candidates":   list,   # 候補リスト（スコア降順）
#     "confidence":      float,  # 0.0〜1.0
#     "roe_benchmark":   float,  # 業態別ROE基準値
#     "roe_excess":      float,  # roe - roe_benchmark（超過リターン）
#     "detection_method":str,    # "financial_override"/"sector_map"/"default"
#   }
#
# 【依存】
#   config.py（BLOCK 25: BM_SECTOR_MAP / BM_DETECTION_THRESHOLDS /
#               BM_ROE_BENCHMARKS / BUSINESS_MODEL_TYPES）
#   追加ライブラリなし（標準ライブラリのみ）
# ==============================================================

from __future__ import annotations

from typing import Optional


# ==============================================================
# BLOCK 0: デフォルト定数（config.py が読めない場合のフォールバック）
# ==============================================================

_DEFAULT_BM_TYPES: dict = {
    "BM-01": "装置産業（重工業・鉄鋼・化学）",
    "BM-02": "消耗品製造（食品・日用品・医薬品）",
    "BM-03": "組立製造（自動車・電機・精密機器）",
    "BM-04": "ソフトウェア・SaaS",
    "BM-05": "プラットフォーム（マーケットプレイス）",
    "BM-06": "金融サービス（銀行・保険・証券）",
    "BM-07": "不動産・REIT",
    "BM-08": "インフラ・公益（電力・ガス・通信・運輸）",
    "BM-09": "小売・流通・卸売",
    "BM-10": "サービス（ヘルスケア・教育・外食）",
    "BM-11": "資源・エネルギー採掘",
    "BM-12": "持株会社・コングロマリット",
}

_DEFAULT_SECTOR_MAP: dict = {
    "1001": ["BM-11"], "1002": ["BM-11"], "1003": ["BM-01"],
    "1004": ["BM-02"], "1005": ["BM-01"], "1006": ["BM-01"],
    "1007": ["BM-01"], "1008": ["BM-02"], "1009": ["BM-11"],
    "1010": ["BM-01"], "1011": ["BM-01"], "1012": ["BM-01"],
    "1013": ["BM-01"], "1014": ["BM-01"], "1015": ["BM-03"],
    "1016": ["BM-03", "BM-04"], "1017": ["BM-03"], "1018": ["BM-03"],
    "1019": ["BM-01"], "1020": ["BM-08"], "1021": ["BM-08"],
    "1022": ["BM-08"], "1023": ["BM-08"], "1024": ["BM-08"],
    "1025": ["BM-08"], "1026": ["BM-09"], "1027": ["BM-09"],
    "1028": ["BM-06"], "1029": ["BM-06"], "1030": ["BM-06"],
    "1031": ["BM-06"], "1032": ["BM-07"], "1033": ["BM-10"],
}

_DEFAULT_ROE_BENCHMARKS: dict = {
    "BM-01": 0.06, "BM-02": 0.10, "BM-03": 0.08, "BM-04": 0.15,
    "BM-05": 0.15, "BM-06": 0.08, "BM-07": 0.05, "BM-08": 0.07,
    "BM-09": 0.08, "BM-10": 0.10, "BM-11": 0.07, "BM-12": 0.06,
}


from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'BMD_DEFAULT_THRESHOLDS': '_DEFAULT_THRESHOLDS',
}

_DEFAULT_THRESHOLDS: dict = {
    "recurring_revenue_ratio_min": 0.70,
    "asset_turnover_heavy_max":    0.50,
    "gross_margin_saas_min":       0.60,
    "debt_ratio_bank_min":         0.80,
    "revenue_per_employee_saas":   20_000_000,
}

_DEFAULT_BM = "BM-10"   # 判定不能時のデフォルト


# ==============================================================
# BLOCK 1: 設定値アクセサ
# ==============================================================

def _get_cfg_dict(cfg, attr: str, default: dict) -> dict:
    """config から辞書定数を安全に取得する。"""
    try:
        val = getattr(cfg, attr, None)
        return val if isinstance(val, dict) else default
    except Exception:
        return default


# ==============================================================
# BLOCK 2: 財務比率オーバーライドルール（Priority 1）
# ==============================================================

def _detect_by_financials(
    data:       dict,
    thresholds: dict,
) -> Optional[tuple[str, float]]:
    """
    財務比率から業態を判定する。
    高い確実性で判定できる場合のみ結果を返す（曖昧な場合は None）。

    Returns:
        (bm_type: str, confidence: float) or None
    """
    try:
        gross_margin    = _f(data.get("gross_margin"))
        debt_ratio      = _f(data.get("debt_ratio"))
        asset_turnover  = _f(data.get("asset_turnover"))
        recurring_ratio = _f(data.get("recurring_revenue_ratio"))
        rev_per_emp     = _f(data.get("revenue_per_employee"))

        th_saas_gm     = float(thresholds.get("gross_margin_saas_min",       0.60))
        th_bank_debt   = float(thresholds.get("debt_ratio_bank_min",          0.80))
        th_heavy_at    = float(thresholds.get("asset_turnover_heavy_max",     0.50))
        th_recur       = float(thresholds.get("recurring_revenue_ratio_min",  0.70))
        th_rev_emp     = float(thresholds.get("revenue_per_employee_saas",    20_000_000))

        # ── Rule 1: SaaS 判定（粗利率 + 売上継続性 or 1人当たり売上）──
        if gross_margin is not None and gross_margin >= th_saas_gm:
            if (recurring_ratio is not None and recurring_ratio >= th_recur):
                return "BM-04", 0.90
            if (rev_per_emp is not None and rev_per_emp >= th_rev_emp):
                return "BM-04", 0.85
            # 粗利率が高いだけなら中程度の確信度
            if gross_margin >= 0.70:
                return "BM-04", 0.65

        # ── Rule 2: 金融業判定（高負債比率）────────────────────────
        if debt_ratio is not None and debt_ratio >= th_bank_debt:
            return "BM-06", 0.88

        # ── Rule 3: 装置産業判定（低資産回転率 + 低粗利率）──────────
        if (asset_turnover is not None and asset_turnover <= th_heavy_at
                and gross_margin is not None and gross_margin < 0.40):
            return "BM-01", 0.82

        # ── Rule 4: プラットフォーム（継続売上高いが粗利率低め）──────
        if (recurring_ratio is not None and recurring_ratio >= th_recur
                and gross_margin is not None and gross_margin < th_saas_gm):
            return "BM-05", 0.75

    except Exception:
        pass

    return None


def _f(val) -> Optional[float]:
    """値を float に変換する。変換できない場合は None。"""
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


# ==============================================================
# BLOCK 3: 業種コードマッピング（Priority 2）
# ==============================================================

def _detect_by_sector_map(
    sector_code: str,
    sector_map:  dict,
) -> tuple[list[str], float]:
    """
    BM_SECTOR_MAP から候補リストと基礎確信度を返す。

    Returns:
        (bm_candidates: list[str], base_confidence: float)
        - 1対1マッピング → confidence 0.80
        - 複数候補 → confidence 0.60（追加判定が必要）
        - 未登録 → ([], 0.0)
    """
    code = str(sector_code).strip()
    candidates = sector_map.get(code, [])
    if not candidates:
        return [], 0.0
    confidence = 0.80 if len(candidates) == 1 else 0.60
    return candidates, confidence


# ==============================================================
# BLOCK 4: ROE超過リターン計算
# ==============================================================

def _calc_roe_excess(
    roe:           Optional[float],
    bm_type:       str,
    roe_benchmarks: dict,
) -> tuple[float, float]:
    """
    業態別ROEベンチマークと超過リターンを計算する。

    Returns:
        (roe_benchmark: float, roe_excess: float)
    """
    try:
        benchmark = float(roe_benchmarks.get(bm_type, 0.08))
        if roe is None:
            return benchmark, 0.0
        roe_f = float(roe)
        # yfinanceのROEはパーセント表記（例: 18.5）と小数（0.185）の両方がある
        if abs(roe_f) > 1.0:
            roe_f = roe_f / 100.0
        excess = round(roe_f - benchmark, 4)
        return benchmark, excess
    except Exception:
        return 0.08, 0.0


# ==============================================================
# BLOCK 5: メインエントリポイント
# ==============================================================

def detect_business_model(
    data:         dict,
    cfg=None,
    sector_code:  str = "",
) -> dict:
    """
    業態（BM-01〜BM-12）を自動判定する。

    Args:
        data:        yfinance / screener データ辞書
                     期待キー: gross_margin, roe, debt_ratio, asset_turnover,
                               recurring_revenue_ratio, revenue_per_employee
        cfg:         config モジュール（None 可）
        sector_code: JP 東証33業種コード（data["sector_code"] が優先）

    Returns:
        {
          "bm_type":          str,
          "bm_label":         str,
          "bm_candidates":    list[dict],  # [{bm_type, label, confidence}]
          "confidence":       float,
          "roe_benchmark":    float,
          "roe_excess":       float,
          "detection_method": str,
        }
    """
    try:
        # ── 設定値取得 ────────────────────────────────────────────
        bm_types        = _get_cfg_dict(cfg, "BUSINESS_MODEL_TYPES",     _DEFAULT_BM_TYPES)
        sector_map      = _get_cfg_dict(cfg, "BM_SECTOR_MAP",            _DEFAULT_SECTOR_MAP)
        roe_benchmarks  = _get_cfg_dict(cfg, "BM_ROE_BENCHMARKS",        _DEFAULT_ROE_BENCHMARKS)
        thresholds      = _get_cfg_dict(cfg, "BM_DETECTION_THRESHOLDS",  _DEFAULT_THRESHOLDS)

        # ── sector_code の決定 ───────────────────────────────────
        code = str(data.get("sector_code", sector_code) or sector_code).strip()

        # ── Priority 1: 財務比率オーバーライド ────────────────────
        fin_result = _detect_by_financials(data, thresholds)
        if fin_result is not None:
            bm_type, confidence = fin_result
            bm_label = bm_types.get(bm_type, bm_type)
            benchmark, excess = _calc_roe_excess(
                _f(data.get("roe")), bm_type, roe_benchmarks
            )
            return {
                "bm_type":          bm_type,
                "bm_label":         bm_label,
                "bm_candidates":    [{"bm_type": bm_type,
                                      "label": bm_label,
                                      "confidence": confidence}],
                "confidence":       confidence,
                "roe_benchmark":    benchmark,
                "roe_excess":       excess,
                "detection_method": "financial_override",
            }

        # ── Priority 2: 業種コードマッピング ────────────────────
        if code:
            candidates, base_conf = _detect_by_sector_map(code, sector_map)
            if candidates:
                bm_type    = candidates[0]   # 先頭が主候補
                bm_label   = bm_types.get(bm_type, bm_type)
                benchmark, excess = _calc_roe_excess(
                    _f(data.get("roe")), bm_type, roe_benchmarks
                )
                cand_list = [
                    {
                        "bm_type":    bm,
                        "label":      bm_types.get(bm, bm),
                        "confidence": base_conf if i == 0 else base_conf * 0.7,
                    }
                    for i, bm in enumerate(candidates)
                ]
                return {
                    "bm_type":          bm_type,
                    "bm_label":         bm_label,
                    "bm_candidates":    cand_list,
                    "confidence":       base_conf,
                    "roe_benchmark":    benchmark,
                    "roe_excess":       excess,
                    "detection_method": "sector_map",
                }

        # ── Priority 3: デフォルト ───────────────────────────────
        bm_label   = bm_types.get(_DEFAULT_BM, _DEFAULT_BM)
        benchmark, excess = _calc_roe_excess(
            _f(data.get("roe")), _DEFAULT_BM, roe_benchmarks
        )
        return {
            "bm_type":          _DEFAULT_BM,
            "bm_label":         bm_label,
            "bm_candidates":    [{"bm_type": _DEFAULT_BM,
                                  "label": bm_label, "confidence": 0.30}],
            "confidence":       0.30,
            "roe_benchmark":    benchmark,
            "roe_excess":       excess,
            "detection_method": "default",
        }

    except Exception as e:
        return {
            "bm_type":          _DEFAULT_BM,
            "bm_label":         _DEFAULT_BM_TYPES.get(_DEFAULT_BM, ""),
            "bm_candidates":    [],
            "confidence":       0.0,
            "roe_benchmark":    0.08,
            "roe_excess":       0.0,
            "detection_method": f"error:{str(e)[:50]}",
        }


# ==============================================================
# BLOCK 6: バッチ処理
# ==============================================================

def detect_batch(
    records:     list[dict],
    cfg=None,
    sector_code_key: str = "sector_code",
) -> list[dict]:
    """
    複数銘柄のデータに対してバッチで業態判定を行う。

    Args:
        records:         [{"ticker": str, "sector_code": str, ...}, ...]
        cfg:             config モジュール
        sector_code_key: sector_code を格納するキー名

    Returns:
        入力 records に業態判定結果を追加したリスト（in-place ではなく新規）
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    results = []
    for rec in records:
        try:
            sector_code = str(rec.get(sector_code_key, ""))
            bm_result   = detect_business_model(rec, cfg, sector_code)
            enriched    = {
                **rec,
                "business_model":    bm_result["bm_type"],
                "bm_label":          bm_result["bm_label"],
                "bm_confidence":     bm_result["confidence"],
                "roe_benchmark":     bm_result["roe_benchmark"],
                "roe_excess":        bm_result["roe_excess"],
                "bm_detection_method": bm_result["detection_method"],
            }
            results.append(enriched)
        except Exception:
            results.append(rec)
    return results


# ==============================================================
# BLOCK 7: 単体テスト
# ==============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("  🏭 business_model_detector.py v1.0 単体テスト")
    print("=" * 60)

    test_cases = [
        {
            "label":       "SaaS企業（粗利率72% + ARR高）",
            "data":        {"gross_margin": 0.72, "recurring_revenue_ratio": 0.85,
                            "roe": 0.20, "sector_code": "1016"},
        },
        {
            "label":       "銀行（高負債比率）",
            "data":        {"debt_ratio": 0.92, "roe": 0.07, "sector_code": "1028"},
        },
        {
            "label":       "鉄鋼（低資産回転率 + 低粗利率）",
            "data":        {"asset_turnover": 0.35, "gross_margin": 0.12,
                            "roe": 0.05, "sector_code": "1012"},
        },
        {
            "label":       "自動車メーカー（業種コードのみ）",
            "data":        {"sector_code": "1017", "roe": 0.10},
        },
        {
            "label":       "不動産（業種コードのみ）",
            "data":        {"sector_code": "1032", "roe": 0.04},
        },
    ]

    for tc in test_cases:
        r = detect_business_model(tc["data"])
        print(f"\n  【{tc['label']}】")
        print(f"    BM タイプ    : {r['bm_type']} — {r['bm_label']}")
        print(f"    確信度      : {r['confidence']:.0%}")
        print(f"    判定方法    : {r['detection_method']}")
        print(f"    ROE基準     : {r['roe_benchmark']:.0%}")
        print(f"    ROE超過     : {r['roe_excess']:+.1%}")
