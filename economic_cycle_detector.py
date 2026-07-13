# ==============================================================
# economic_cycle_detector.py  v1.0  (D-1 / ダリオ経済サイクル)
# 経済サイクルフェーズ自動判定 — 4象限マトリクス
# ==============================================================
# 【役割】
#   GDP成長率 × インフレ率（CPI）の2軸で現在の経済フェーズを判定し、
#   composite_score のセクター補正量を算出する。
#   run_all_tasks() の macro ブロック直後・sector 分析直前に実行。
#
# 【4象限定義（ダリオ "Economic Machine" 理論）】
#   象限A: 成長↑ × インフレ↑  → コモディティ/資源/不動産優位
#   象限B: 成長↑ × インフレ↓  → 株式（特に成長株）最強局面
#   象限C: 成長↓ × インフレ↑  → 物価連動資産優位（スタグフレーション）
#   象限D: 成長↓ × インフレ↓  → 債券優位・現金比率UP（デフレ懸念）
#
# 【データソース（優先順）】
#   GDP成長率 : IMF DataMapper キャッシュ → FRED（GDPC1前期比）→ 推定値
#   CPI変化率 : gov_data_pipeline（e-Stat）→ FRED（CPIAUCSL）→ macro_snapshot
#   補助指標   : bond_commodity の recession_score / イールドカーブ形状
#
# 【出力スキーマ】
#   {
#     "phase":        str,    # "A"/"B"/"C"/"D"
#     "phase_label":  str,    # "成長↑×インフレ↑（コモディティ優位）"
#     "gdp_growth":   float,  # 使用したGDP成長率（前期比%）
#     "cpi_change":   float,  # 使用したCPI変化率（前期比%）
#     "confidence":   float,  # 判定の確信度 0.0〜1.0
#     "data_source":  str,    # "imf_cache" / "fred" / "gov" / "estimated"
#     "sector_adjustments": dict,  # {sector_code: delta_pt}
#     "phase_history":      list,  # 過去12ヶ月のフェーズ推移
#     "transition_alert":   bool,  # フェーズ遷移が起きたか
#     "summary_text":       str,   # weekly_report 向け説明文
#   }
#
# 【composite_score への連動】
#   macro_snapshot_for_sector["_macro_phase"] に格納し、
#   base_agent.build_result() がこれを読んでセクター補正を適用。
#   補正は config.py の CYCLE_PHASE_SECTOR_ADJUSTMENTS で管理。
#
# 【依存】config.py / results dict（gov/imf/macro_snapshot/bond_commodity）
# ==============================================================

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


# ==============================================================
# BLOCK 1: データ取得ヘルパー
# ==============================================================

def _get_gdp_growth(results: dict, cfg) -> tuple[Optional[float], str]:
    """
    GDP成長率（前期比%）を優先順で取得する。

    Returns:
        (gdp_growth_pct, source_label)
        取得失敗時は (None, "unavailable")
    """
    # ① IMFキャッシュから取得（imf_pipeline.py が保存した JSON）
    try:
        imf_cache_path = Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / "imf_cache.json"
        if imf_cache_path.exists():
            imf_data = json.loads(imf_cache_path.read_text(encoding="utf-8"))
            # NGDP_RPCH = Real GDP growth rate (percent change)
            # ★ 2026-07-12是正: imf_pipeline._save_imf_cache の実構造は
            #   {"indicators": {indicator: {country: {year: val}}}} なのに
            #   {country: {indicator: ...}} を読んでいて常時失敗→GDP N/A だった
            #   （producer/consumerキー不整合）。実構造を優先し旧想定は後段に残す。
            _ind = imf_data.get("indicators", {}) or {}
            jpn = ((_ind.get("NGDP_RPCH", {}) or {}).get("JPN", {})
                   or imf_data.get("JPN", {}).get("NGDP_RPCH", {}))
            if jpn:
                # 最新年のデータを取得
                years = sorted(jpn.keys(), reverse=True)
                for yr in years:
                    v = jpn[yr]
                    if v is not None:
                        return float(v), "imf_cache"
    except Exception:
        pass

    # ② results["imf"] から取得（当日実行済みの場合）
    try:
        imf_res = results.get("imf", {})
        if isinstance(imf_res, dict) and not imf_res.get("skipped"):
            indicators = imf_res.get("indicators", {})
            jpn_gdp = indicators.get("JPN", {}).get("NGDP_RPCH")
            if jpn_gdp is not None:
                return float(jpn_gdp), "imf_result"
    except Exception:
        pass

    # ③ FRED API から取得（GDPC1: 米国実質GDP前期比年率）
    try:
        fred_key = getattr(cfg, "POLICY_FRED_API_KEY", "") or ""
        if fred_key:
            import requests
            r = requests.get(
                "https://api.stlouisfed.org/fred/series/observations",
                params={
                    "series_id":    "A191RL1Q225SBEA",  # 実質GDP成長率（季調済年率）
                    "api_key":      fred_key,
                    "file_type":    "json",
                    "sort_order":   "desc",
                    "limit":        4,
                    "observation_start": (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d"),
                },
                timeout=10,
            )
            if r.status_code == 200:
                obs = [o for o in r.json().get("observations", [])
                       if o.get("value", ".") != "."]
                if obs:
                    # 年率 → 前期比に変換（÷4 の近似）
                    annual_rate = float(obs[0]["value"])
                    return round(annual_rate / 4, 3), "fred"
    except Exception:
        pass

    # ④ macro_snapshot の nikkei/sp500 トレンドから推定（最終フォールバック）
    try:
        snap = results.get("macro_snapshot", {})
        sp500 = snap.get("sp500")
        if sp500 is not None:
            # S&P500が年初来+10%超なら成長拡大相当と推定
            # ※ 精度は低いが「データなし」より有用
            # この推定は confidence を低くする
            return None, "unavailable"
    except Exception:
        pass

    return None, "unavailable"


def _get_cpi_change(results: dict, cfg) -> tuple[Optional[float], str]:
    """
    CPI変化率（前月比 or 前期比%）を優先順で取得する。

    Returns:
        (cpi_change_pct, source_label)
    """
    # ① gov_data_pipeline の結果から取得
    try:
        gov = results.get("gov", {})
        if isinstance(gov, dict):
            # ★ 2026-07-12是正: gov v9.0 の実構造は
            #   {"stat_results": [{"stat": {"id": "cpi", ...}, "changes": {...}}, ...]}
            #   なのに gov["cpi"] を読んでいて常時失敗→CPI N/A だった。
            for _sr in gov.get("stat_results", []) or []:
                if ((_sr.get("stat", {}) or {}).get("id") == "cpi"):
                    _ch = _sr.get("changes", {}) or {}
                    _change = (_ch.get("yoy_change")
                               if _ch.get("yoy_change") is not None
                               else _ch.get("mom_change"))
                    if _change is not None:
                        return float(_change), "gov_pipeline"
            # 旧想定の構造（フォールバックとして残置）
            cpi_data = gov.get("cpi", {})
            if isinstance(cpi_data, dict):
                # change_pct または前期比を探す
                change = cpi_data.get("change_pct") or cpi_data.get("yoy_change")
                if change is not None:
                    return float(change), "gov_pipeline"
                # 直近2値から計算
                values = cpi_data.get("values", [])
                if len(values) >= 2 and values[-1] and values[-2]:
                    yoy = (float(values[-1]) / float(values[-2]) - 1) * 100
                    return round(yoy, 3), "gov_pipeline_calc"
    except Exception:
        pass

    # ② FRED API から取得（CPIAUCSL: 米国CPI / JPNXD2723 は日本CPI代替）
    try:
        fred_key = getattr(cfg, "POLICY_FRED_API_KEY", "") or ""
        if fred_key:
            import requests
            r = requests.get(
                "https://api.stlouisfed.org/fred/series/observations",
                params={
                    "series_id":    "CPIAUCSL",  # 米国CPI（日本CPIの代替・相関高）
                    "api_key":      fred_key,
                    "file_type":    "json",
                    "sort_order":   "desc",
                    "limit":        14,
                    "frequency":    "m",
                },
                timeout=10,
            )
            if r.status_code == 200:
                obs = [o for o in r.json().get("observations", [])
                       if o.get("value", ".") != "."]
                if len(obs) >= 13:
                    current = float(obs[0]["value"])
                    year_ago = float(obs[12]["value"])
                    yoy = round((current / year_ago - 1) * 100, 3)
                    return yoy, "fred_cpi"
    except Exception:
        pass

    # ③ macro_snapshot からインフレ代理変数として gold/oil を使用
    try:
        snap = results.get("macro_snapshot", {})
        # bond_commodity の recession_score が高い → インフレ懸念と関連
        bc = results.get("bond_commodity", {})
        if isinstance(bc, dict):
            curve = bc.get("curve_analysis", {})
            if isinstance(curve, dict):
                # 逆イールド → デフレ懸念としてCPI低下を推定
                if curve.get("is_inverted_2y10y"):
                    return None, "unavailable"
    except Exception:
        pass

    return None, "unavailable"


# ==============================================================
# BLOCK 2: フェーズ判定
# ==============================================================

# 各フェーズの定義
PHASE_DEFINITIONS = {
    "A": {
        "label":    "成長↑×インフレ↑（コモディティ・資源優位）",
        "emoji":    "🟠",
        "description": (
            "景気拡大かつインフレ上昇局面。コモディティ・資源・不動産が相対的に強い。"
            "輸出企業は追い風だが、コスト上昇で消費財は逆風。"
        ),
    },
    "B": {
        "label":    "成長↑×インフレ↓（株式最強局面）",
        "emoji":    "🟢",
        "description": (
            "景気拡大かつインフレ安定。株式（特に成長株・輸出株）が最強。"
            "金融緩和余地があり、全セクターに追い風が吹きやすい「ゴルディロックス」局面。"
        ),
    },
    "C": {
        "label":    "成長↓×インフレ↑（スタグフレーション・最悪期）",
        "emoji":    "🔴",
        "description": (
            "景気後退かつインフレ継続。物価連動資産・コモディティが優位。"
            "株式・債券ともに不利で最も困難なフェーズ。防衛・現金比率を高める。"
        ),
    },
    "D": {
        "label":    "成長↓×インフレ↓（債券優位・デフレ懸念）",
        "emoji":    "🔵",
        "description": (
            "景気後退かつインフレ低下。長期債券が相対的に優位。"
            "日本が1990-2012年に経験した局面。ディフェンシブ銘柄に資金が集まる。"
        ),
    },
}


def _determine_phase(
    gdp_growth: Optional[float],
    cpi_change: Optional[float],
    recession_score: int,
    cfg,
) -> tuple[str, float]:
    """
    GDP成長率 × CPI変化率から経済フェーズを判定する。

    Args:
        gdp_growth      : 前期比GDP成長率（%）。Noneの場合は補助指標で推定。
        cpi_change      : 前期比CPI変化率（%）。Noneの場合は補助指標で推定。
        recession_score : bond_commodity_pipeline の recession_score（0〜100）
        cfg             : config モジュール

    Returns:
        (phase: str, confidence: float)
    """
    gdp_threshold = getattr(cfg, "CYCLE_GDP_GROWTH_THRESHOLD", 0.0)  # 成長/後退の境目
    cpi_threshold = getattr(cfg, "CYCLE_CPI_CHANGE_THRESHOLD", 2.0)  # インフレ/デフレの境目

    confidence = 1.0

    # ── GDP成長率の判定 ──────────────────────────────────────
    if gdp_growth is not None:
        growth_up = gdp_growth > gdp_threshold
    else:
        # recession_score で代替推定（精度低下）
        confidence *= 0.6
        growth_up = recession_score < 45  # リセッションリスクが低い = 成長継続と推定

    # ── CPI変化率の判定 ──────────────────────────────────────
    if cpi_change is not None:
        inflation_up = cpi_change > cpi_threshold
    else:
        # recession_scoreと逆イールドから推定（精度低下）
        confidence *= 0.6
        # recession_score高＝デフレ懸念として暫定判定
        inflation_up = recession_score < 30  # 低リスク=インフレ継続の可能性

    # ── フェーズ確定 ──────────────────────────────────────────
    if growth_up and inflation_up:
        phase = "A"
    elif growth_up and not inflation_up:
        phase = "B"
    elif not growth_up and inflation_up:
        phase = "C"
    else:
        phase = "D"

    # recession_score が極端に高い場合は C or D に強制
    if recession_score >= 70 and phase not in ("C", "D"):
        phase = "C" if inflation_up else "D"
        confidence *= 0.7
        print(f"    ⚠️  recession_score={recession_score}により象限を{phase}に補正")
    # ── [RESEARCH-1] NOTE: confidence は regime_detector.py が後段で補正する ──
    # main.py の HMM ブロックで d1_confidence_adj を加算するため
    # ここでは D-1 単独の信頼度のみを返す（変更不要）
    return phase, round(confidence, 2)


# ==============================================================
# BLOCK 3: セクター補正量算出
# ==============================================================

def _calc_sector_adjustments(phase: str, cfg) -> dict:
    """
    現在の経済フェーズに基づき、各業種の composite_score 補正量を返す。

    config.py の CYCLE_PHASE_SECTOR_ADJUSTMENTS から読み込む。
    設定がない場合はデフォルト値を使用。

    Returns:
        {sector_code: delta_pt}
    """
    # config に定義があればそちらを優先
    user_adj = getattr(cfg, "CYCLE_PHASE_SECTOR_ADJUSTMENTS", {})
    if user_adj and phase in user_adj:
        return user_adj[phase]

    # ── デフォルト補正テーブル（ダリオ理論に基づく）──────────────
    DEFAULT_ADJUSTMENTS = {
        # 象限A: 成長↑×インフレ↑
        # → コモディティ/資源/金融に追い風 / 消費財/輸送に逆風
        "A": {
            "1009": +8,   # 石油・石炭   : エネルギー高で最も恩恵
            "1002": +6,   # 鉱業         : 資源高
            "1012": +5,   # 鉄鋼         : 資源高・インフレ転嫁
            "1013": +5,   # 非鉄金属
            "1028": +4,   # 銀行         : 金利上昇で利鞘拡大
            "1029": +3,   # 証券
            "1032": +3,   # 不動産       : インフレヘッジ
            "1004": -4,   # 食料品       : 原料コスト上昇
            "1021": -3,   # 陸運         : 燃料費上昇
            "1023": -5,   # 空運         : 燃料費直撃
            "1027": -3,   # 小売         : コスト転嫁困難
        },
        # 象限B: 成長↑×インフレ↓ （ゴルディロックス）
        # → グロース・輸出全般に追い風 / 特段の逆風なし
        "B": {
            "1016": +5,   # 電気機器     : グロース最強局面
            "1017": +5,   # 輸送用機器   : 輸出採算改善
            "1025": +5,   # 情報通信     : グロース
            "1018": +4,   # 精密機器
            "1015": +4,   # 機械         : 設備投資増加
            "1028": +3,   # 銀行         : 景気拡大で貸出増
            "1032": +3,   # 不動産       : 低金利継続
            "1004": +2,   # 食料品       : コスト安定
        },
        # 象限C: 成長↓×インフレ↑ （スタグフレーション）
        # → 全体に逆風。コモディティのみ逃げ場。ディフェンシブ相対優位
        "C": {
            "1009": +4,   # 石油・石炭   : 資源高（唯一の避難先）
            "1008": +3,   # 医薬品       : ディフェンシブ
            "1004": +2,   # 食料品       : ディフェンシブ
            "1020": +2,   # 電気・ガス
            "1016": -8,   # 電気機器     : 景気敏感・売られる
            "1017": -8,   # 輸送用機器
            "1015": -6,   # 機械
            "1032": -6,   # 不動産       : 金利高+景気後退
            "1028": -5,   # 銀行         : 不良債権リスク
            "1027": -5,   # 小売
            "1021": -4,   # 陸運
            "1023": -5,   # 空運
        },
        # 象限D: 成長↓×インフレ↓ （デフレ懸念）
        # → 債券・ディフェンシブ優位 / 景気敏感・資源に逆風
        "D": {
            "1008": +5,   # 医薬品       : 不況無関係
            "1004": +4,   # 食料品       : 需要安定
            "1020": +4,   # 電気・ガス
            "1033": +2,   # サービス     : 一部ディフェンシブ
            "1009": -6,   # 石油・石炭   : 資源安
            "1002": -5,   # 鉱業
            "1012": -5,   # 鉄鋼
            "1013": -4,   # 非鉄金属
            "1016": -5,   # 電気機器     : 設備投資凍結
            "1017": -6,   # 輸送用機器   : 耐久財需要後退
            "1015": -5,   # 機械
            "1028": -4,   # 銀行         : 利鞘縮小・不良債権
            "1022": -4,   # 海運         : 貿易量減少
        },
    }

    return DEFAULT_ADJUSTMENTS.get(phase, {})


# ==============================================================
# BLOCK 4: フェーズ履歴管理
# ==============================================================

def _load_phase_history(cfg) -> list:
    """
    過去のフェーズ判定履歴を読み込む。
    保存先: DATA_DIR/cycle_phase_history.json
    形式: [{"date": "YYYY-MM-DD", "phase": "B", "gdp": 0.5, "cpi": 1.2}, ...]
    保持: 直近12ヶ月分
    """
    path = Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / "cycle_phase_history.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def _save_phase_history(cfg, today: str, phase: str,
                        gdp: Optional[float], cpi: Optional[float]) -> list:
    """
    今回のフェーズ判定を履歴に追記して保存する。
    古いエントリ（12ヶ月超）は自動削除。

    Returns:
        更新後の履歴リスト
    """
    history = _load_phase_history(cfg)

    # 同日エントリは上書き
    history = [h for h in history if h.get("date") != today]
    history.append({
        "date":  today,
        "phase": phase,
        "gdp":   gdp,
        "cpi":   cpi,
    })

    # 12ヶ月分（約365日）のみ保持
    cutoff = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
    history = [h for h in history if h.get("date", "") >= cutoff]
    history = sorted(history, key=lambda x: x.get("date", ""))

    path = Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / "cycle_phase_history.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"    ⚠️  cycle_phase_history 保存失敗: {e}")

    return history


def _detect_phase_transition(history: list, current_phase: str) -> bool:
    """
    直前のフェーズと今回のフェーズが異なる場合 True を返す。
    初回（履歴なし）は False。
    """
    if len(history) < 2:
        return False
    # 最新エントリは今回のもの（すでに追記済み）なので1つ前と比較
    prev = history[-2].get("phase")
    return prev is not None and prev != current_phase


# ==============================================================
# BLOCK 5: サマリーテキスト生成
# ==============================================================

def _format_summary(
    phase: str,
    phase_label: str,
    gdp: Optional[float],
    cpi: Optional[float],
    confidence: float,
    adjustments: dict,
    transition: bool,
    history: list,
    sector_names: dict,
) -> str:
    """weekly_report 向け Markdown テキストを生成する。"""
    pdef = PHASE_DEFINITIONS.get(phase, {})
    emoji = pdef.get("emoji", "⚪")
    desc  = pdef.get("description", "")

    gdp_s = f"{gdp:+.2f}%" if gdp is not None else "データ取得中"
    cpi_s = f"{cpi:+.2f}%" if cpi is not None else "データ取得中"
    conf_s = f"{confidence*100:.0f}%"

    lines = [
        f"## 🌐 経済サイクルフェーズ判定（ダリオ4象限）",
        "",
        f"**現在フェーズ**: {emoji} **象限{phase}** — {phase_label}",
        f"**判定根拠**: GDP成長率 {gdp_s} / CPI変化率 {cpi_s} / 確信度 {conf_s}",
        "",
        f"> {desc}",
        "",
    ]

    if transition:
        lines += [
            "⚠️ **フェーズ遷移を検出** — 前回から象限が変化しました。",
            "セクターウェイトの見直しを推奨します。",
            "",
        ]

    # フェーズ履歴（直近6ヶ月）
    if len(history) >= 2:
        recent = history[-6:]
        hist_str = " → ".join(
            f"{h['date'][5:]}:{h['phase']}" for h in recent
        )
        lines.append(f"**フェーズ推移（直近{len(recent)}期）**: {hist_str}")
        lines.append("")

    # 補正上位3業種の表示
    if adjustments:
        up3   = sorted([(c, d) for c, d in adjustments.items() if d > 0],
                       key=lambda x: -x[1])[:3]
        down3 = sorted([(c, d) for c, d in adjustments.items() if d < 0],
                       key=lambda x: x[1])[:3]
        if up3:
            names = [f"{sector_names.get(c, c)}({d:+.0f}pt)" for c, d in up3]
            lines.append(f"💚 **追い風業種**: {' / '.join(names)}")
        if down3:
            names = [f"{sector_names.get(c, c)}({d:+.0f}pt)" for c, d in down3]
            lines.append(f"🔴 **逆風業種**: {' / '.join(names)}")
        lines.append("")

    return "\n".join(lines)


# ==============================================================
# BLOCK 6: メインエントリポイント
# ==============================================================

def run_economic_cycle_detector(cfg, results: dict) -> dict:
    """
    経済サイクルフェーズを判定し、セクター補正量を算出する。

    stock_analyzer_main.py から以下のように呼び出す:
        _ec = _load_local_module("economic_cycle_detector", ...)
        ec_result = _ec.run_economic_cycle_detector(cfg, results)
        results["economic_cycle"] = ec_result
        # macro_snapshot_for_sector に象限情報を埋め込む
        macro_snapshot_for_sector["_macro_phase"] = ec_result.get("phase", "B")

    Returns: 上記スキーマの dict
    """
    if not getattr(cfg, "ENABLE_ECONOMIC_CYCLE", True):
        print("  ⏩ 経済サイクル判定: スキップ（ENABLE_ECONOMIC_CYCLE=False）")
        return {"skipped": True, "phase": "B"}  # デフォルトは象限B（最も一般的）

    print("\n  🌐 経済サイクルフェーズ判定中...")
    today = datetime.now().strftime("%Y-%m-%d")

    # ── データ取得 ───────────────────────────────────────────
    gdp_growth, gdp_source = _get_gdp_growth(results, cfg)
    cpi_change, cpi_source  = _get_cpi_change(results, cfg)

    # bond_commodity の recession_score を補助情報として取得
    bc = results.get("bond_commodity", {})
    recession_score = bc.get("recession_score", 30) if isinstance(bc, dict) else 30

    print(f"    GDP成長率: {f'{gdp_growth:+.2f}%' if gdp_growth is not None else 'N/A'}"
          f"（{gdp_source}）")
    print(f"    CPI変化率: {f'{cpi_change:+.2f}%' if cpi_change is not None else 'N/A'}"
          f"（{cpi_source}）")
    print(f"    リセッションスコア: {recession_score}/100（補助）")

    # ── フェーズ判定 ─────────────────────────────────────────
    phase, confidence = _determine_phase(gdp_growth, cpi_change, recession_score, cfg)
    pdef       = PHASE_DEFINITIONS[phase]
    phase_label = pdef["label"]

    # ── セクター補正量算出 ────────────────────────────────────
    adjustments = _calc_sector_adjustments(phase, cfg)

    # ── 履歴更新 ─────────────────────────────────────────────
    history   = _save_phase_history(cfg, today, phase, gdp_growth, cpi_change)
    transition = _detect_phase_transition(history, phase)

    # ── 業種名マップ ─────────────────────────────────────────
    sector_names = {
        code: info.get("name_ja", code)
        for code, info in getattr(cfg, "JAPAN_SECTORS", {}).items()
    }

    # ── サマリーテキスト生成 ──────────────────────────────────
    summary_text = _format_summary(
        phase, phase_label, gdp_growth, cpi_change,
        confidence, adjustments, transition, history, sector_names,
    )

    # ── ログ出力 ─────────────────────────────────────────────
    emoji = pdef.get("emoji", "⚪")
    print(f"    ✅ 象限{phase}: {emoji} {phase_label}")
    print(f"       確信度: {confidence*100:.0f}%"
          f" / 補正対象: {len(adjustments)}業種"
          f" / フェーズ遷移: {'あり⚠️' if transition else 'なし'}")
    if adjustments:
        top3_up = sorted([(c, d) for c, d in adjustments.items() if d > 0],
                         key=lambda x: -x[1])[:3]
        if top3_up:
            names = [f"{sector_names.get(c,c)}({d:+.0f}pt)" for c, d in top3_up]
            print(f"       追い風: {' / '.join(names)}")

    return {
        "phase":              phase,
        "phase_label":        phase_label,
        "phase_emoji":        pdef.get("emoji", "⚪"),
        "gdp_growth":         gdp_growth,
        "cpi_change":         cpi_change,
        "gdp_source":         gdp_source,
        "cpi_source":         cpi_source,
        "recession_score":    recession_score,
        "confidence":         confidence,
        "sector_adjustments": adjustments,
        "phase_history":      history[-12:],  # 直近12件
        "transition_alert":   transition,
        "summary_text":       summary_text,
        "timestamp":          datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "skipped":            False,
    }
