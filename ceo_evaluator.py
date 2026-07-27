# ==============================================================
# ceo_evaluator.py  v1.0  (I-3 / 井村俊哉モデル)
# 経営者スコア自動算出
# ==============================================================
# 【役割】
#   「小型株では事業モデルより経営者で結果が変わる」という
#   井村俊哉氏の原則を4軸で定量化する。
#
# 【4軸評価（各0〜25pt / 合計0〜100pt）】
#   軸①: 就任後の業績改善実績（営業利益CAGR + ROE改善幅）
#   軸②: 公約達成率（中期経営計画の達成度）
#   軸③: 資本配分の合理性（M&A成功率 + 内部留保の使い方）
#   軸④: コミュニケーションの一貫性（保守度スコアI-2と連動）
#
# 【データソース】
#   軸①: edinet_rotation_manager DB の financials
#         + yfinance の営業利益・ROEデータ
#   軸②: edinet_rotation_manager DB の midterm_plan（中計）
#   軸③: yfinance の goodwill_impairment / capex / FCF 等
#   軸④: earnings_surprise.py の conservatism_score（I-2）
#
# 【社長交代の特別処理】
#   就任1年未満: スコア計算対象外（"観察中" フラグ）
#   就任2〜3年:  暫定スコア（軸①のみ）
#   就任4年以上: フルスコア計算
#
# 【出力スキーマ】
#   {
#     "ceo_score":             float,  # 0〜100
#     "ceo_label":             str,
#     "tenure_years":          float,
#     "status":                str,    # "full" / "provisional" / "observing" / "unknown"
#     "axis_scores":           dict,   # {axis1..4: score}
#     "flags":                 list,
#     "management_renewal":    bool,   # 社長交代 × activist_score 高
#     "data_sources":          list,
#   }
#
# 【依存】config.py / edinet_rotation_manager DB / yfinance
# ==============================================================

from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional


# ==============================================================
# BLOCK 0: 定数
# ==============================================================

CEO_SCORES_FILENAME  = "ceo_scores.json"
SCORES_RETAIN_YEARS  = 5   # 5年分の経営者スコアを保持

# 就任年数による評価制限

from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'CEO_TENURE_OBSERVING_MAX': 'TENURE_OBSERVING_MAX',
    'CEO_TENURE_FULL_MIN': 'TENURE_FULL_MIN',
    'CEO_ACTIVIST_RENEWAL_THRESHOLD': 'ACTIVIST_RENEWAL_THRESHOLD',
    'B4_CEO_EVALUATOR_SCORES_RETAIN_YEARS': 'SCORES_RETAIN_YEARS',
    'B6_CEO_EVALUATOR_CAGR_EXCELLENT': 'CAGR_EXCELLENT',
    'B6_CEO_EVALUATOR_CAGR_GOOD': 'CAGR_GOOD',
    'B6_CEO_EVALUATOR_CAGR_NEUTRAL': 'CAGR_NEUTRAL',
    'B6_CEO_EVALUATOR_MIDTERM_EXCELLENT': 'MIDTERM_EXCELLENT',
    'B6_CEO_EVALUATOR_MIDTERM_GOOD': 'MIDTERM_GOOD',
    'B6_CEO_EVALUATOR_MIDTERM_FAIR': 'MIDTERM_FAIR',
}

TENURE_OBSERVING_MAX  = 1.0   # 1年未満 → 観察中
TENURE_FULL_MIN       = 4.0   # 4年以上 → フルスコア

# 軸①の閾値
CAGR_EXCELLENT = 0.15   # 営業利益CAGR 15%/年超 → 25pt
CAGR_GOOD      = 0.08   # 8%/年超 → 15pt
CAGR_NEUTRAL   = 0.00   # 0%以上 → 6pt

# 軸②の閾値
MIDTERM_EXCELLENT = 0.90   # 90%以上達成
MIDTERM_GOOD      = 0.70   # 70%以上達成
MIDTERM_FAIR      = 0.50   # 50%以上達成

# 社長交代 × アクティビスト連携
ACTIVIST_RENEWAL_THRESHOLD = 60.0   # activist_score がこれ以上で「経営刷新期待」


# ==============================================================
# BLOCK 1: DB I/O
# ==============================================================

def _db_path(cfg) -> Path:
    return Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / CEO_SCORES_FILENAME


def load_ceo_db(cfg) -> dict:
    """
    ceo_scores.json を読み込む。
    構造: {ticker: {ceo_name: str, scores_history: [...], latest: {...}}}
    """
    p = _db_path(cfg)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_ceo_db(cfg, db: dict) -> None:
    p = _db_path(cfg)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"  ⚠️  ceo_scores.json 保存失敗: {e}")


def _upsert_ceo_record(cfg, ticker: str, result: dict) -> None:
    """今回のスコア算出結果を ceo_scores.json に追記・更新する。"""
    db    = load_ceo_db(cfg)
    today = date.today().strftime("%Y-%m-%d")

    entry = db.get(ticker, {
        "ticker":         ticker,
        "scores_history": [],
        "latest":         {},
    })

    # 同日エントリは上書き
    entry["scores_history"] = [
        h for h in entry.get("scores_history", [])
        if h.get("date") != today
    ]
    entry["scores_history"].append({
        "date":      today,
        "ceo_score": result.get("ceo_score"),
        "status":    result.get("status"),
        "ceo_name":  result.get("ceo_name", ""),
    })

    # 5年（約260週）分保持
    cutoff = (date.today() - timedelta(days=365 * SCORES_RETAIN_YEARS)).strftime("%Y-%m-%d")
    entry["scores_history"] = [
        h for h in entry["scores_history"] if h.get("date", "") >= cutoff
    ]

    entry["latest"]     = result
    entry["updated_at"] = today
    db[ticker]          = entry
    save_ceo_db(cfg, db)


# ==============================================================
# BLOCK 2: EDINETデータ取得ヘルパー
# ==============================================================

def _get_edinet_financials(ticker: str, edinet_db: dict) -> dict:
    """
    edinet_rotation_manager の蓄積DBから財務データを取得する。
    """
    analysis = edinet_db.get("analysis", {}).get(ticker, {})
    fin      = analysis.get("financials", {}) if isinstance(analysis, dict) else {}
    return fin


def _get_ceo_tenure(ticker: str, edinet_db: dict) -> tuple[Optional[str], Optional[float]]:
    """
    EDINETから代表取締役の就任年度を取得し、就任年数を返す。

    Returns:
        (ceo_name: str | None, tenure_years: float | None)
    """
    analysis = edinet_db.get("analysis", {}).get(ticker, {})
    if not isinstance(analysis, dict):
        return None, None

    # edinet_rotation_manager が蓄積する想定のキー
    officers = analysis.get("officers", {})
    ceo_data = officers.get("representative_director", {})

    if not ceo_data:
        return None, None

    ceo_name      = ceo_data.get("name")
    appointed_str = ceo_data.get("appointed_date", "")

    if not appointed_str:
        return ceo_name, None

    try:
        appointed = datetime.strptime(appointed_str[:10], "%Y-%m-%d").date()
        tenure    = (date.today() - appointed).days / 365.25
        return ceo_name, round(tenure, 1)
    except Exception:
        return ceo_name, None


# ==============================================================
# BLOCK 3: 軸① 就任後の業績改善実績
# ==============================================================

def _axis1_performance(
    ticker: str,
    tenure_years: Optional[float],
    edinet_db: dict,
) -> tuple[float, dict]:
    """
    軸①: 就任後の営業利益CAGR + ROE改善幅でスコアを算出する。

    データ: edinet_rotation_manager の financials.operating_income（リスト）
            yfinance の returnOnEquity で補完

    Returns:
        (score: float, detail: dict)  0〜25pt
    """
    fin = _get_edinet_financials(ticker, edinet_db)
    detail = {}
    score  = 0.0

    # 営業利益の時系列（リスト形式: 古い順）
    op_income_list = fin.get("operating_income", [])
    op_income_list = [v for v in op_income_list if v is not None]

    cagr = None
    if tenure_years and tenure_years >= 1.0 and len(op_income_list) >= 2:
        # 就任後期間に対応するデータ点数
        n_years  = min(int(tenure_years), len(op_income_list) - 1)
        base_val = op_income_list[-(n_years + 1)]
        end_val  = op_income_list[-1]
        if base_val and base_val > 0 and end_val:
            try:
                cagr = (end_val / base_val) ** (1 / n_years) - 1
            except Exception:
                cagr = None

    # ── 営業利益CAGR スコア ──────────────────────────────
    if cagr is not None:
        if   cagr > CAGR_EXCELLENT: axis1_base = 25.0
        elif cagr > CAGR_GOOD:      axis1_base = 15.0
        elif cagr > CAGR_NEUTRAL:   axis1_base =  6.0
        else:                        axis1_base =  0.0
        detail["op_income_cagr"] = round(cagr * 100, 1)
        detail["cagr_score"]     = axis1_base
    else:
        axis1_base = 6.0   # データなし → 中立
        detail["op_income_cagr"] = None

    # ── ROE改善幅スコア ──────────────────────────────────
    roe_adj  = 0.0
    roe_list = fin.get("roe_history", [])
    roe_list = [v for v in roe_list if v is not None]

    if tenure_years and len(roe_list) >= 2:
        n       = min(int(tenure_years), len(roe_list) - 1)
        roe_old = roe_list[-(n + 1)]
        roe_now = roe_list[-1]
        if roe_old is not None and roe_now is not None:
            roe_delta = (roe_now - roe_old) * 100   # パーセントポイント
            if   roe_delta >= 5.0:  roe_adj = 10.0
            elif roe_delta >= -2.0: roe_adj =  3.0
            else:                   roe_adj = -5.0
            detail["roe_delta_pt"] = round(roe_delta, 1)
            detail["roe_adj"]      = roe_adj

    score = round(max(0.0, min(25.0, axis1_base + roe_adj)), 1)
    detail["axis1_score"] = score
    return score, detail


# ==============================================================
# BLOCK 4: 軸② 公約達成率（中計）
# ==============================================================
def _axis2_midterm_plan(ticker: str, edinet_db: dict, cfg: dict | None = None) -> tuple[float, dict]:
    """
    軸②: 中期経営計画の達成率でスコアを算出する。
    midterm_plan_tracker.py を優先参照し、なければ EDINET DB から取得。
    データなし → 中立（12pt）

    Returns:
        (score: float, detail: dict)  0〜25pt
    """
    # ── [I-5] midterm_plan_tracker.py の DB を優先参照 ──────────
    if cfg is not None:
        try:
            import sys as _sys, importlib as _imp
            from pathlib import Path
            _sys.path.insert(0, str(Path(__file__).parent))
            _mt = _imp.import_module("midterm_plan_tracker")
            axis2_score = _mt.get_ceo_axis2_score(cfg, ticker)
            avg_rate    = _mt.get_achievement_rate(cfg, ticker)
            detail      = {
                "source":           "midterm_plan_tracker",
                "avg_achievement":  avg_rate,
                "axis2_score":      axis2_score,
            }
            return axis2_score, detail
        except Exception:
            pass   # フォールバック: 以下の既存ロジックを使用

    # ── フォールバック: EDINET DB から直接取得 ─────────────────
    analysis  = edinet_db.get("analysis", {}).get(ticker, {})
    midterm   = analysis.get("midterm_plan", {}) if isinstance(analysis, dict) else {}
    detail    = {}

    if not midterm:
        detail["midterm_available"] = False
        return 12.0, detail   # データなし → 中立

    # 達成率 (0.0〜1.0)
    achievement = midterm.get("achievement_rate")
    if achievement is None:
        return 12.0, detail

    detail["midterm_available"]    = True
    detail["achievement_rate_pct"] = round(achievement * 100, 1)

    if   achievement >= MIDTERM_EXCELLENT: base = 25.0
    elif achievement >= MIDTERM_GOOD:      base = 15.0
    elif achievement >= MIDTERM_FAIR:      base =  6.0
    else:                                  base =  0.0

    # 未達時の説明品質
    explanation_bonus = 0.0
    if achievement < MIDTERM_GOOD:
        quality = midterm.get("explanation_quality", "none")
        # "good" / "vague" / "none" のいずれかを想定
        if   quality == "good":  explanation_bonus =  3.0
        elif quality == "vague": explanation_bonus = -3.0
        else:                    explanation_bonus = -5.0
        detail["explanation_quality"]  = quality
        detail["explanation_bonus"]    = explanation_bonus

    score = round(max(0.0, min(25.0, base + explanation_bonus)), 1)
    detail["axis2_score"] = score
    return score, detail


# ==============================================================
# BLOCK 5: 軸③ 資本配分の合理性
# ==============================================================

def _axis3_capital_allocation(ticker: str, edinet_db: dict) -> tuple[float, dict]:
    """
    軸③: M&A成功率・内部留保の使い方でスコアを算出する。

    データ:
      goodwill_impairment: のれん減損（M&A失敗の代理変数）
      fcf_margin:          FCFマージン（フリーキャッシュフロー体質）
      net_cash_ratio:      ネットキャッシュ / 時価総額（M-1）
      payout_ratio:        配当性向

    Returns:
        (score: float, detail: dict)  0〜25pt
    """
    fin    = _get_edinet_financials(ticker, edinet_db)
    detail = {}
    score  = 12.0   # データ不足時はデフォルト中立

    # ── M&A成功率（のれん減損の有無で代替） ──────────────
    goodwill_impairment = fin.get("goodwill_impairment")
    total_assets        = fin.get("total_assets")

    ma_score = 12.0
    if goodwill_impairment is not None and total_assets:
        impairment_ratio = goodwill_impairment / total_assets
        if   impairment_ratio <= 0.001: ma_score = 25.0  # 減損ほぼなし = 成功
        elif impairment_ratio <= 0.005: ma_score = 15.0
        elif impairment_ratio <= 0.02:  ma_score =  6.0
        else:                           ma_score =  0.0  # 大規模減損 = 失敗
        detail["impairment_ratio_pct"] = round(impairment_ratio * 100, 3)
        detail["ma_score"]             = ma_score

    # ── 内部留保の使い方 ─────────────────────────────────
    retention_adj = 0.0
    net_cash_ratio = fin.get("net_cash_ratio")   # M-1と連動
    payout_ratio   = fin.get("payout_ratio")

    if net_cash_ratio is not None and payout_ratio is not None:
        # 過度な内部留保 × 低還元 → 減点
        if net_cash_ratio > 0.30 and payout_ratio < 0.20:
            retention_adj = -5.0
            detail["retention_flag"] = "現金積み上げのみ（投資も還元もしない）"
        # 適切なバランス
        elif 0.05 <= net_cash_ratio <= 0.30 and 0.20 <= payout_ratio <= 0.60:
            retention_adj = 5.0
            detail["retention_flag"] = "成長投資と株主還元のバランス良好"
        # 高還元（成熟企業型）
        elif payout_ratio >= 0.50:
            retention_adj = 3.0
            detail["retention_flag"] = "積極的株主還元"

    score = round(max(0.0, min(25.0, ma_score + retention_adj)), 1)
    detail["axis3_score"]    = score
    detail["retention_adj"]  = retention_adj
    return score, detail


# ==============================================================
# BLOCK 6: 軸④ コミュニケーションの一貫性
# ==============================================================

def _axis4_communication(
    ticker: str,
    edinet_db: dict,
    conservatism_score: Optional[float] = None,
) -> tuple[float, dict]:
    """
    軸④: コミュニケーションの一貫性をスコア化する。

    保守度スコア（I-2）を主要シグナルとして活用:
      conservatism_score は「経営者の発言と実績の乖離パターン」の
      定量化であり、軸④の代理変数として最適。

    補助指標:
      ir_quality: IRの質（edinet_db の ir_quality キー）
        "active"   → 個人向け説明会・SNS等 → +8pt
        "standard" → 最低限のみ            → +0pt
        "minimal"  → 形式的のみ            → -3pt

    Returns:
        (score: float, detail: dict)  0〜25pt
    """
    detail = {}

    # ── 保守度スコア（I-2）からの変換 ───────────────────
    if conservatism_score is not None:
        # conservatism_score 0〜100 → 0〜20pt に変換
        comm_base = conservatism_score * 0.20
        detail["conservatism_score"] = conservatism_score
        detail["comm_base"]          = round(comm_base, 1)
    else:
        comm_base = 10.0   # データなし → 中立（50 × 0.20）
        detail["conservatism_score"] = None

    # ── IRの質による補正 ──────────────────────────────────
    analysis   = edinet_db.get("analysis", {}).get(ticker, {})
    ir_quality = analysis.get("ir_quality", "standard") if isinstance(analysis, dict) else "standard"

    ir_bonus_map = {"active": 5.0, "standard": 0.0, "minimal": -3.0}
    ir_bonus = ir_bonus_map.get(ir_quality, 0.0)
    detail["ir_quality"] = ir_quality
    detail["ir_bonus"]   = ir_bonus

    score = round(max(0.0, min(25.0, comm_base + ir_bonus)), 1)
    detail["axis4_score"] = score
    return score, detail


# ==============================================================
# BLOCK 7: 社長交代検知と特別処理
# ==============================================================

def _check_management_renewal(
    ticker: str,
    tenure_years: Optional[float],
    activist_score: Optional[float],
    results: dict,
) -> tuple[bool, str]:
    """
    社長交代フラグと「経営刷新期待」フラグを判定する。

    Returns:
        (management_renewal: bool, renewal_note: str)
    """
    # TDnetの MGMT_CHANGE イベントとの照合
    tdnet = results.get("tdnet", {})
    mgmt_events = [ev for ev in tdnet.get("events", [])
                   if ev.get("category") == "MGMT_CHANGE"
                   and ev.get("ticker") == ticker]

    # 就任1年未満も「最近の交代」と見なす
    recent_change = bool(mgmt_events) or (tenure_years is not None and tenure_years < 1.0)

    if not recent_change:
        return False, ""

    # activist_score が高い場合は「経営刷新期待」
    renewal_note = ""
    activist_high = (activist_score or 0) >= ACTIVIST_RENEWAL_THRESHOLD

    if activist_high:
        renewal_note = (
            f"🔄 経営刷新期待銘柄 "
            f"（社長交代 × activist_score={activist_score:.0f}pt）"
        )
        return True, renewal_note

    return False, "社長交代あり（アクティビスト連携なし）"


# ==============================================================
# BLOCK 8: 総合スコア算出
# ==============================================================

def evaluate_ceo(
    ticker:             str,
    edinet_db:          dict,
    cfg                 = None,
    conservatism_score: Optional[float] = None,
    activist_score:     Optional[float] = None,
    results:            dict            = None,
) -> dict:
    """
    1銘柄の経営者スコアを4軸で算出する。

    Args:
        ticker             : 銘柄コード（例: "6758.T"）
        edinet_db          : edinet_rotation_manager.load_db(cfg) の戻り値
        cfg                : config モジュール
        conservatism_score : I-2 の conservatism_score（軸④に使用）
        activist_score     : M-3 の activist_score（社長交代連携に使用）
        results            : run_all_tasks() の results dict（TDnet連携）

    Returns:
        上記スキーマの dict
    """
    results = results or {}

    # ── 経営者就任年数の取得 ──────────────────────────────
    ceo_name, tenure_years = _get_ceo_tenure(ticker, edinet_db)
    detail_all = {
        "ceo_name":     ceo_name,
        "tenure_years": tenure_years,
    }

    # ── 評価可能か判断 ───────────────────────────────────
    if tenure_years is None:
        status = "unknown"
    elif tenure_years < TENURE_OBSERVING_MAX:
        status = "observing"
    elif tenure_years < TENURE_FULL_MIN:
        status = "provisional"
    else:
        status = "full"

    # ── 社長交代チェック ─────────────────────────────────
    min_tenure = getattr(cfg, "CEO_MIN_TENURE_YEARS", 2) if cfg else 2
    is_renewal, renewal_note = _check_management_renewal(
        ticker, tenure_years, activist_score, results
    )

    # ── 各軸の算出（status に応じて計算範囲を変える）──────
    axis1_score, detail1 = _axis1_performance(ticker, tenure_years, edinet_db)
    detail_all["axis1"] = detail1

    if status == "observing":
        # 観察中 → 全軸を中立に設定
        axis1_score  = 0.0
        axis2_score  = 0.0
        axis3_score  = 0.0
        axis4_score  = 0.0
        ceo_score    = 0.0
        ceo_label    = "📋 観察中（就任1年未満）"

    elif status == "provisional":
        # 暫定 → 軸①のみ有効、他は中立
        axis2_score, detail2 = 12.0, {"provisional": True}
        axis3_score, detail3 = 12.0, {"provisional": True}
        axis4_score, detail4 = _axis4_communication(ticker, edinet_db, conservatism_score)
        detail_all.update({"axis2": detail2, "axis3": detail3, "axis4": detail4})

        ceo_score = round(
            axis1_score * 1.0          # 軸①のみ実スコア
            + axis2_score * 0.5        # 軸②③は中立の50%加算
            + axis3_score * 0.5
            + axis4_score,
            1,
        )
        ceo_score = round(min(75.0, ceo_score), 1)   # 暫定の上限は75pt
        ceo_label = f"📊 暫定スコア（就任{tenure_years:.0f}年・軸①+④）"

    else:
        # フルスコア
        axis2_score, detail2 = _axis2_midterm_plan(ticker, edinet_db)
        axis3_score, detail3 = _axis3_capital_allocation(ticker, edinet_db)
        axis4_score, detail4 = _axis4_communication(ticker, edinet_db, conservatism_score)
        detail_all.update({"axis2": detail2, "axis3": detail3, "axis4": detail4})

        ceo_score = round(
            axis1_score + axis2_score + axis3_score + axis4_score,
            1,
        )
        ceo_score = round(min(100.0, ceo_score), 1)

        # ラベル生成
        if   ceo_score >= 80: ceo_label = "🌟 卓越した経営者"
        elif ceo_score >= 65: ceo_label = "📈 優良経営者"
        elif ceo_score >= 50: ceo_label = "➡️ 標準的経営者"
        elif ceo_score >= 35: ceo_label = "⚠️ 改善余地あり"
        else:                  ceo_label = "🔴 要注意経営者"

    # ── quality_score への補正量 ──────────────────────────
    # ceo_score 80以上: +HIGH / 65〜79: +MID / 50〜64: +2pt / 50未満: 0pt
    # 2026-07-25 バッチ3: 補正量を config.py BLOCK I-3 から読むよう是正。
    # 従来は 10.0 / 5.0 を直書きしており、config の CEO_QUALITY_BONUS_* を
    # 編集しても補正量は変わらなかった（SSOT違反）。
    # 既定値は従来の直書き値なので挙動は不変。
    _q_high = float(getattr(cfg, "CEO_QUALITY_BONUS_HIGH", 10.0))
    _q_mid  = float(getattr(cfg, "CEO_QUALITY_BONUS_MID",   5.0))
    if ceo_score >= 80:      quality_bonus = _q_high
    elif ceo_score >= 65:    quality_bonus = _q_mid
    elif ceo_score >= 50:    quality_bonus =  2.0
    elif status == "observing": quality_bonus = 0.0
    else:                    quality_bonus =  0.0

    flags = []
    if is_renewal and renewal_note:
        flags.append(renewal_note)
    if status == "observing":
        flags.append("📋 就任1年未満（観察期間）")
    if ceo_score >= 80 and status == "full":
        flags.append("🌟 高スコア経営者（quality_score +10pt）")

    return {
        "ticker":            ticker,
        "ceo_name":          ceo_name or "不明",
        "ceo_score":         ceo_score,
        "ceo_label":         ceo_label,
        "tenure_years":      tenure_years,
        "status":            status,
        "axis_scores": {
            "axis1_performance":       axis1_score,
            "axis2_midterm":           axis2_score if status != "observing" else 0.0,
            "axis3_capital":           axis3_score if status != "observing" else 0.0,
            "axis4_communication":     axis4_score if status != "observing" else 0.0,
        },
        "quality_bonus":     quality_bonus,
        "flags":             flags,
        "management_renewal": is_renewal,
        "renewal_note":      renewal_note,
        "detail":            detail_all,
        "data_sources":      ["edinet_db", "yfinance", "conservatism_score"],
        "timestamp":         datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ==============================================================
# BLOCK 9: バッチ処理
# ==============================================================

def run_ceo_evaluator(
    cfg,
    edinet_db:        dict,
    tickers:          list,
    surprise_cache:   dict = None,
    screener_result:  dict = None,
    results:          dict = None,
) -> dict:
    """
    複数銘柄の経営者スコアを一括算出する。

    stock_analyzer_main.py から screener 直後に呼び出す:
        _ce = _load_local_module("ceo_evaluator", ...)
        ce_result = _ce.run_ceo_evaluator(cfg, edinet_db, tickers,
                                           surprise_cache=..., results=results)
        results["ceo_scores"] = ce_result

    Args:
        cfg            : config モジュール
        edinet_db      : edinet_rotation_manager.load_db(cfg)
        tickers        : 分析対象のtickerリスト
        surprise_cache : earnings_surprise.run_earnings_surprise() の "results" キー
        screener_result: run_stock_screener() の戻り値（activist_score取得用）
        results        : run_all_tasks() の results dict

    Returns:
        {
          "results":     {ticker: evaluate_ceo()の戻り値},
          "top5":        [ticker, ...],  # ceo_score 上位5
          "renewal_candidates": [ticker, ...],  # 経営刷新期待
          "total":       int,
          "timestamp":   str,
        }
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    print("\n  👔 経営者スコア算出 開始...")
    t0 = datetime.now()

    # activist_score を screener から取得
    activist_map = {}
    if screener_result:
        for c in screener_result.get("candidates", []):
            tk = c.get("ticker", "")
            if tk:
                activist_map[tk] = c.get("activist_score", 0)

    # conservatism_score を earnings_surprise から取得
    conservatism_map = {}
    if surprise_cache:
        for tk, sr in surprise_cache.items():
            conservatism_map[tk] = sr.get("conservatism_score", 50.0)

    eval_results     = {}
    renewal_candidates = []

    for ticker in tickers:
        try:
            # EDINETデータが存在する銘柄のみ評価（データなしはスキップ）
            analysis = edinet_db.get("analysis", {}).get(ticker, {})
            if not analysis:
                continue

            result = evaluate_ceo(
                ticker             = ticker,
                edinet_db          = edinet_db,
                cfg                = cfg,
                conservatism_score = conservatism_map.get(ticker),
                activist_score     = activist_map.get(ticker),
                results            = results or {},
            )
            eval_results[ticker] = result

            if result.get("management_renewal"):
                renewal_candidates.append(ticker)

            # DB に永続化
            _upsert_ceo_record(cfg, ticker, result)

            time.sleep(0.05)   # DB書き込みのスロットル

        except Exception as e:
            print(f"    ⚠️  {ticker} CEO評価エラー: {str(e)[:60]}")

    # 上位5銘柄（フルスコアのみ）
    full_scores = {
        tk: r["ceo_score"] for tk, r in eval_results.items()
        if r.get("status") == "full"
    }
    top5 = sorted(full_scores, key=full_scores.get, reverse=True)[:5]

    elapsed = (datetime.now() - t0).seconds
    print(
        f"    ✅ 経営者スコア完了（{elapsed}秒）: "
        f"{len(eval_results)}銘柄評価 "
        f"| 経営刷新期待{len(renewal_candidates)}社"
    )
    if top5:
        for tk in top5[:3]:
            r = eval_results[tk]
            print(f"       {tk} {r['ceo_name']}: {r['ceo_score']:.0f}pt "
                  f"({r['ceo_label']})")

    return {
        "results":            eval_results,
        "top5":               top5,
        "renewal_candidates": renewal_candidates,
        "total":              len(eval_results),
        "timestamp":          t0.strftime("%Y-%m-%d %H:%M:%S"),
    }
