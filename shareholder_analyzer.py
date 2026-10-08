# ==============================================================
# shareholder_analyzer.py  v1.0  (P-4)
# 株主構成分析 — 片山晃 × 村上世彰モデル
# ==============================================================
# 【役割】
#   「誰が持っているか」を定量化する4軸分析。
#   ① 機関投資家カバレッジ（情報優位度）
#   ② 外国人持株比率のトレンド（先行指標）
#   ③ 持ち合い解消進行度（村上モデル連携）
#   ④ 創業家・オーナー持株比率（長期経営者の有無）
#
# 【データソース】
#   edinet_rotation_manager の蓄積 DB（analysis/{ticker}.json）
#   → "shareholder" サブキーに以下が格納されていることを前提とする:
#      {
#        "institutional_pct":  float,  # 機関保有比率（%）
#        "foreign_pct":        float,  # 外国人保有比率（%）
#        "individual_pct":     float,  # 個人保有比率（%）
#        "top10_shareholders": [
#          {"name": str, "type": "institution"/"foreign"/"corporate"/"individual",
#           "pct": float}, ...
#        ],
#        "as_of_date":         str,    # "YYYY-MM-DD"
#      }
#
# 【出力スキーマ】
#   analyze_shareholder(ticker, edinet_data) → {
#     "shareholder_score":    float,   # 0〜100
#     "institutional_pct":    float,
#     "foreign_pct":          float,
#     "owner_pct":            float,
#     "policy_share_trend":   str,     # "decreasing"/"stable"/"increasing"
#     "axes":                 dict,    # 4軸スコア詳細
#     "flags":                list,    # 検知フラグ
#     "foreign_spike_alert":  bool,    # 外国人急増アラート
#     "activist_entry_alert": bool,    # アクティビスト参戦アラート
#     "discovery_adj":        float,   # discovery_score への補正量
#   }
#
# 【改修が必要な関連ファイル（差分指示参照）】
#   edinet_rotation_manager.py / stock_screener.py / tdnet_monitor.py
#   alert_notifier.py / config.py
#
# 【依存】config.py / edinet_rotation_manager DB（間接）
# ==============================================================

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional


# ==============================================================
# BLOCK 0: 定数
# ==============================================================

SHAREHOLDER_HISTORY_FILE = "shareholder_history.json"
HISTORY_RETAIN_QUARTERS  = 8   # 2年分を保持

# 機関保有比率の閾値

from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'SHA_INST_HIGH_THRESHOLD': 'INST_HIGH_THRESHOLD',
    'SHA_INST_LOW_THRESHOLD': 'INST_LOW_THRESHOLD',
    'B4_SHAREHOLDER_ANALYZER_HISTORY_RETAIN_QUARTERS': 'HISTORY_RETAIN_QUARTERS',
    'B6_SHAREHOLDER_ANALYZER_FOREIGN_SPIKE_PCT': 'FOREIGN_SPIKE_PCT',
    'B6_SHAREHOLDER_ANALYZER_FOREIGN_SURGE_PCT': 'FOREIGN_SURGE_PCT',
    'B6_SHAREHOLDER_ANALYZER_ACTIVIST_HOLDINGS_PCT': 'ACTIVIST_HOLDINGS_PCT',
}

INST_HIGH_THRESHOLD = 70.0   # 70%超 → 発見済み（α取得困難）
INST_LOW_THRESHOLD  = 20.0   # 20%未満 → 情報優位高

# 外国人持株変化の閾値
FOREIGN_SPIKE_PCT       = 3.0   # 前年比+3%以上 → 「外国人発見フェーズ」
FOREIGN_SURGE_PCT       = 5.0   # 前年比+5%以上 → アラート発火
ACTIVIST_HOLDINGS_PCT   = 5.0   # 5%超 → アクティビスト参戦アラート


# ==============================================================
# BLOCK 1: 履歴 I/O
# ==============================================================

def _hist_path(cfg) -> Path:
    return Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / SHAREHOLDER_HISTORY_FILE


def load_shareholder_history(cfg) -> dict:
    """
    shareholder_history.json を読み込む。
    形式: {ticker: [{as_of_date, institutional_pct, foreign_pct, ...}, ...]}
    """
    p = _hist_path(cfg)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_shareholder_history(cfg, history: dict) -> None:
    p = _hist_path(cfg)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"  ⚠️  shareholder_history 保存失敗: {e}")


def _append_history(cfg, ticker: str, snapshot: dict) -> None:
    """
    今回の株主構成スナップショットを履歴に追記する。
    同一 as_of_date は上書き。HISTORY_RETAIN_QUARTERS 超は自動削除。
    """
    history = load_shareholder_history(cfg)
    records = history.get(ticker, [])

    as_of = snapshot.get("as_of_date", date.today().strftime("%Y-%m-%d"))
    records = [r for r in records if r.get("as_of_date") != as_of]
    records.append(snapshot)
    records.sort(key=lambda x: x.get("as_of_date", ""))

    # 直近8四半期のみ保持
    if len(records) > HISTORY_RETAIN_QUARTERS:
        records = records[-HISTORY_RETAIN_QUARTERS:]

    history[ticker] = records
    save_shareholder_history(cfg, history)


# ==============================================================
# BLOCK 2: 4軸スコア計算
# ==============================================================

def _axis1_institutional_coverage(institutional_pct: Optional[float]) -> tuple[float, str]:
    """
    軸①: 機関投資家カバレッジ（情報優位度）

    機関保有比率が低い = 情報優位高（片山モデル適合）
    → discovery_score を補強する。

    Returns:
        (score: float, label: str)  0〜25pt
    """
    if institutional_pct is None:
        return 12.5, "機関比率データなし（中立）"

    if institutional_pct < INST_LOW_THRESHOLD:
        return 25.0, f"機関比率{institutional_pct:.0f}%→情報優位高（片山モデル適合）"
    elif institutional_pct < 30.0:
        return 20.0, f"機関比率{institutional_pct:.0f}%→やや情報優位"
    elif institutional_pct < 60.0:
        return 12.5, f"機関比率{institutional_pct:.0f}%→標準的カバレッジ"
    elif institutional_pct < INST_HIGH_THRESHOLD:
        return 5.0,  f"機関比率{institutional_pct:.0f}%→発見済み傾向（α取得難化）"
    else:
        return 0.0,  f"機関比率{institutional_pct:.0f}%→完全発見済み"


def _axis2_foreign_trend(
    foreign_pct: Optional[float],
    history: list,
    cfg = None,
) -> tuple[float, str, bool, bool]:
    """
    軸②: 外国人持株比率のトレンド

    外国人増加 = ガバナンス改善の先行指標（バリュー・ガバナンス重視の機関が買う）
    外国人5%超単一株主 = アクティビスト参戦アラート

    Returns:
        (score: float, label: str, foreign_spike_alert: bool, activist_alert: bool)
    """
    foreign_spike_alert  = False
    activist_alert       = False

    if foreign_pct is None:
        return 12.5, "外国人比率データなし（中立）", False, False

    # アクティビスト参戦チェック（単一外国人株主が5%超）は
    # top10_shareholders から別途チェックする
    # ここでは外国人全体比率のトレンドのみ評価

    # 過去データとの比較
    yoy_change = None
    if len(history) >= 4:   # 4四半期以上 = 約1年前と比較可能
        prev_foreign = history[-4].get("foreign_pct")
        if prev_foreign is not None:
            yoy_change = foreign_pct - prev_foreign

    # 発見フェーズの閾値は config.py BLOCK P-4 の FOREIGN_INVESTOR_SPIKE_PCT が正本。
    # モジュール定数 FOREIGN_SPIKE_PCT は config に無いときの既定値。
    _spike_pct = float(getattr(cfg, "FOREIGN_INVESTOR_SPIKE_PCT",
                               FOREIGN_SPIKE_PCT))
    if yoy_change is not None:
        if yoy_change >= FOREIGN_SURGE_PCT:
            score = 25.0
            label = f"外国人{foreign_pct:.0f}%（前年比+{yoy_change:.1f}%→急増）"
            foreign_spike_alert = True
        elif yoy_change >= _spike_pct:
            score = 22.0
            label = f"外国人{foreign_pct:.0f}%（前年比+{yoy_change:.1f}%→発見フェーズ）"
        elif yoy_change >= 1.0:
            score = 18.0
            label = f"外国人{foreign_pct:.0f}%（前年比+{yoy_change:.1f}%→増加傾向）"
        elif yoy_change <= -3.0:
            score = 5.0
            label = f"外国人{foreign_pct:.0f}%（前年比{yoy_change:.1f}%→減少）"
        else:
            score = 12.5
            label = f"外国人{foreign_pct:.0f}%（安定）"
    else:
        # 比較データなし: 現在の水準のみで評価
        if foreign_pct >= 30.0:
            score = 18.0
            label = f"外国人高比率{foreign_pct:.0f}%（グローバル注目）"
        elif foreign_pct >= 15.0:
            score = 12.5
            label = f"外国人{foreign_pct:.0f}%（標準水準）"
        else:
            score = 10.0
            label = f"外国人{foreign_pct:.0f}%（低水準・発見余地）"

    return score, label, foreign_spike_alert, activist_alert


def _axis3_policy_share_trend(top10: list, history: list) -> tuple[float, str, str]:
    """
    軸③: 持ち合い解消進行度（村上モデル連携）

    大株主上位10名のうち事業法人（corporate）が減少傾向
    → 持ち合い解消が進行中 = アクティビスト戦略の実現可能性が高まる

    Returns:
        (score: float, label: str, trend: "decreasing"/"stable"/"increasing")
    """
    if not top10:
        return 12.5, "持ち合い状況データなし（中立）", "stable"

    # 現在の事業法人比率を算出
    corp_pct = sum(
        s.get("pct", 0) for s in top10
        if s.get("type") == "corporate"
    )

    # 過去との比較
    trend = "stable"
    if len(history) >= 2:
        prev_top10 = history[-2].get("top10_shareholders", [])
        prev_corp  = sum(
            s.get("pct", 0) for s in prev_top10
            if s.get("type") == "corporate"
        )
        change = corp_pct - prev_corp
        if change <= -2.0:
            trend = "decreasing"
        elif change >= 2.0:
            trend = "increasing"

    if trend == "decreasing":
        score = 22.0
        label = f"持ち合い解消進行中（事業法人{corp_pct:.0f}%→減少）"
    elif trend == "increasing":
        score = 5.0
        label = f"持ち合い増加（事業法人{corp_pct:.0f}%→増加・解放困難）"
    else:
        if corp_pct < 10.0:
            score = 18.0
            label = f"持ち合い少（事業法人{corp_pct:.0f}%→すでに希薄）"
        elif corp_pct > 40.0:
            score = 8.0
            label = f"持ち合い多（事業法人{corp_pct:.0f}%→解放余地大）"
        else:
            score = 12.5
            label = f"持ち合い標準（事業法人{corp_pct:.0f}%）"

    return score, label, trend


def _axis4_owner_pct(top10: list) -> tuple[float, str]:
    """
    軸④: 創業家・オーナー持株比率（長期経営 × 外部圧力への耐性）

    オーナー系経営者が大株主 → 長期視点での意思決定が可能
    完全管理職経営 × 低ガバナンス → ガバナンスリスク

    Returns:
        (score: float, label: str)  0〜25pt
    """
    if not top10:
        return 12.5, "オーナー情報データなし（中立）"

    # "individual" かつ 経営幹部関連の名称を持つ株主をオーナーと推定
    # ※ EDINET大株主データでは type="individual" でかつ代表者名が含まれる場合を利用
    owner_pct = sum(
        s.get("pct", 0) for s in top10
        if s.get("type") == "individual" and s.get("is_officer", False)
    )

    # type="individual"全体の比率でも補完
    all_individual_pct = sum(
        s.get("pct", 0) for s in top10 if s.get("type") == "individual"
    )

    pct = max(owner_pct, all_individual_pct * 0.5)   # 保守的に個人全体の半分と推定

    if pct >= 20.0:
        return 25.0, f"オーナー経営（個人大株主{pct:.0f}%→長期視点確保）"
    elif pct >= 10.0:
        return 18.0, f"オーナー系要素あり（個人{pct:.0f}%）"
    elif pct >= 5.0:
        return 12.5, f"準オーナー型（個人{pct:.0f}%）"
    else:
        return 8.0, f"純粋管理職経営（個人株主{pct:.0f}%→ガバナンス注視）"


# ==============================================================
# BLOCK 3: アクティビスト参戦検知
# ==============================================================

def _check_activist_entry(top10: list, cfg = None) -> tuple[bool, Optional[dict]]:
    """
    単一の外国人・機関投資家が 5%超を保有していないか確認する。
    TDnet の大量保有報告書と連動する（将来拡張）。
    """
    threshold = getattr(cfg, "ACTIVIST_HOLDINGS_THRESHOLD", ACTIVIST_HOLDINGS_PCT / 100)
    if threshold < 1:
        threshold *= 100   # 0.05 → 5.0 %に変換

    for s in top10:
        pct  = s.get("pct", 0)
        stype = s.get("type", "")
        name  = s.get("name", "")
        if pct >= threshold and stype in ("foreign", "institution"):
            return True, {
                "name":  name,
                "pct":   pct,
                "type":  stype,
                "alert": f"アクティビスト参戦疑い: {name}（{pct:.1f}%保有）",
            }
    return False, None


# ==============================================================
# BLOCK 4: 総合スコア計算
# ==============================================================

def analyze_shareholder(
    ticker:      str,
    edinet_data: dict,
    cfg = None,
    history_records: list = None,
) -> dict:
    """
    1銘柄の株主構成を4軸で分析してスコアを返す。

    Args:
        ticker       : 銘柄コード（例: "6758.T"）
        edinet_data  : edinet_rotation_manager DB のうち当該銘柄のデータ
                       期待するキー: edinet_data["analysis"]["shareholder"]
        cfg          : config モジュール
        history_records : 当該銘柄の過去履歴（load_shareholder_history から取得）

    Returns:
        上記スキーマの dict
    """
    history_records = history_records or []

    # EDINET データから株主情報を取得
    analysis   = edinet_data.get("analysis", {}) if isinstance(edinet_data, dict) else {}
    shareholder = analysis.get("shareholder", {})

    inst_pct    = shareholder.get("institutional_pct")
    foreign_pct = shareholder.get("foreign_pct")
    top10       = shareholder.get("top10_shareholders", [])
    as_of       = shareholder.get("as_of_date", "")

    # 4軸スコア計算
    axis1_score, axis1_label = _axis1_institutional_coverage(inst_pct)
    axis2_score, axis2_label, foreign_spike_alert, _ = _axis2_foreign_trend(
        foreign_pct, history_records, cfg
    )
    axis3_score, axis3_label, policy_trend = _axis3_policy_share_trend(
        top10, history_records
    )
    axis4_score, axis4_label = _axis4_owner_pct(top10)

    # アクティビスト参戦チェック
    activist_alert, activist_detail = _check_activist_entry(top10, cfg)

    # 総合スコア（4軸の合計 → 最大100pt）
    total = axis1_score + axis2_score + axis3_score + axis4_score
    total = round(min(100.0, max(0.0, total)), 1)

    # discovery_score への補正量（情報優位・外国人発見フェーズは正の補正）
    discovery_adj = 0.0
    if inst_pct is not None and inst_pct < INST_LOW_THRESHOLD:
        discovery_adj += 5.0    # 情報優位銘柄 → discovery_score +5pt
    if foreign_spike_alert:
        discovery_adj += 3.0    # 外国人急増 → +3pt

    # フラグ生成
    flags = []
    if inst_pct is not None and inst_pct < INST_LOW_THRESHOLD:
        flags.append(f"情報優位銘柄（機関{inst_pct:.0f}%）")
    if foreign_spike_alert:
        flags.append("🌏外国人発見フェーズ")
    if activist_alert:
        flags.append(f"⚡{activist_detail['alert']}")
    if policy_trend == "decreasing":
        flags.append("🔓持ち合い解消進行")
    if axis4_score >= 18:
        flags.append("👑オーナー経営")

    return {
        "ticker":              ticker,
        "shareholder_score":   total,
        "institutional_pct":   inst_pct,
        "foreign_pct":         foreign_pct,
        "owner_pct":           sum(s.get("pct", 0) for s in top10
                                   if s.get("type") == "individual"),
        "policy_share_trend":  policy_trend,
        "as_of_date":          as_of,
        "axes": {
            "institutional_coverage": {
                "score": axis1_score, "label": axis1_label, "max": 25
            },
            "foreign_trend": {
                "score": axis2_score, "label": axis2_label, "max": 25
            },
            "policy_share_trend": {
                "score": axis3_score, "label": axis3_label, "max": 25
            },
            "owner_ratio": {
                "score": axis4_score, "label": axis4_label, "max": 25
            },
        },
        "flags":               flags,
        "foreign_spike_alert": foreign_spike_alert,
        "activist_entry_alert": activist_alert,
        "activist_detail":      activist_detail,
        "discovery_adj":        discovery_adj,
    }


# ==============================================================
# BLOCK 5: バッチ処理（screener 連携）
# ==============================================================

def run_shareholder_analyzer(cfg, edinet_db: dict, tickers: list = None) -> dict:
    """
    複数銘柄の株主構成を一括分析する。

    stock_analyzer_main.py から screener の直後に呼び出す:
        _sa = _load_local_module("shareholder_analyzer", ...)
        sa_result = _sa.run_shareholder_analyzer(cfg, edinet_db,
                                                  tickers=screener_tickers)
        results["shareholder"] = sa_result

    Args:
        cfg       : config モジュール
        edinet_db : edinet_rotation_manager.load_db(cfg) の戻り値
                    {"analysis": {ticker: analysis_dict}, ...}
        tickers   : 分析対象 ticker リスト（省略時は edinet_db の全銘柄）

    Returns:
        {
          "results":           {ticker: analyze_shareholder() の戻り値},
          "foreign_spike":     [ticker, ...],  # 外国人急増
          "activist_entries":  [ticker, ...],  # アクティビスト参戦疑い
          "info_advantage":    [ticker, ...],  # 情報優位（機関比率低）
          "total":             int,
          "timestamp":         str,
        }
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    print("\n  👥 株主構成分析 開始...")
    t0 = datetime.now()

    analysis_db = edinet_db.get("analysis", {}) if isinstance(edinet_db, dict) else {}
    if tickers is None:
        tickers = list(analysis_db.keys())

    history_all = load_shareholder_history(cfg)
    results     = {}
    foreign_spike   = []
    activist_entries = []
    info_advantage  = []

    for ticker in tickers:
        edinet_data = analysis_db.get(ticker, {})
        if not edinet_data or not edinet_data.get("shareholder"):
            continue

        hist_records = history_all.get(ticker, [])
        result       = analyze_shareholder(ticker, {"analysis": edinet_data},
                                           cfg, hist_records)
        results[ticker] = result

        # スナップショットを履歴に追記
        snap = {
            "as_of_date":      result["as_of_date"],
            "institutional_pct": result["institutional_pct"],
            "foreign_pct":     result["foreign_pct"],
            "owner_pct":       result["owner_pct"],
            "policy_trend":    result["policy_share_trend"],
        }
        if snap["as_of_date"]:
            _append_history(cfg, ticker, snap)

        if result["foreign_spike_alert"]:
            foreign_spike.append(ticker)
        if result["activist_entry_alert"]:
            activist_entries.append(ticker)
        if (result["institutional_pct"] or 100) < 20.0:
            info_advantage.append(ticker)

    elapsed = (datetime.now() - t0).seconds
    print(f"    ✅ 株主分析完了（{elapsed}秒）: "
          f"{len(results)}銘柄 "
          f"| 外国人急増{len(foreign_spike)} "
          f"| 情報優位{len(info_advantage)}"
          + (f" | ⚡アクティビスト{len(activist_entries)}!" if activist_entries else ""))

    # [P-4] shareholder_score 上位N（件数は config.SHAREHOLDER_SCORE_TOP_N）。
    # 既存3リスト（外国人急増／アクティビスト／情報優位）はフラグでの分類で、
    # スコア順の一覧はそれとは別物。
    _top_n = int(getattr(cfg, "SHAREHOLDER_SCORE_TOP_N", 10))
    top_scored = [
        {"ticker": t,
         "shareholder_score": r.get("shareholder_score", 0.0),
         "institutional_pct": r.get("institutional_pct"),
         "foreign_pct":       r.get("foreign_pct")}
        for t, r in sorted(
            results.items(),
            key=lambda kv: kv[1].get("shareholder_score", 0.0) or 0.0,
            reverse=True,
        )[:_top_n]
    ]

    return {
        "results":           results,
        "top_scored":        top_scored,
        "foreign_spike":     foreign_spike,
        "activist_entries":  activist_entries,
        "info_advantage":    info_advantage,
        "total":             len(results),
        "timestamp":         t0.strftime("%Y-%m-%d %H:%M:%S"),
    }
