# ==============================================================
# stock_analyzer_main.py  v3.0
# 自律型投資AIエージェント基盤 — メイン制御 + Streamlit UI
# ==============================================================
# 【v2.0 からの変更点サマリー】
#
#   [ADD-1] config.py 参照に統一
#     v2.0: DRIVE_BASE 等の定数を本ファイルに直書き
#     v3.0: import config as cfg で全設定を参照
#
#   [ADD-2] マクロ指標取得・追い風/逆風判定
#     v2.0: この機能が存在しなかった
#     v3.0: fetch_macro_snapshot() / evaluate_macro_signals() / get_sector_macro_icon()
#
#   [ADD-3] 33業種エージェント 動的ロードインターフェース（雛形）
#     v2.0: この機能が存在しなかった
#     v3.0: load_sector_agent() / run_sector_analysis()
#           sector_agents/{code}_agent.py を置くだけで自動検出・実行
#
#   [ADD-4] Streamlit UI の骨組み
#     v2.0: UI なし（Markdown ファイルのみ）
#     v3.3: Tab1を3内部タブ化（マクロシグナル/長期トレンド/短期デイリー）
#
#   [ADD-5] マクロ分析レポートの Markdown 保存
#     v2.0: なし
#     v3.0: _save_macro_report() を追加
#
# 【依存関係】
#   import: config.py / arxiv_monitor.py（動的ロード）
#   ★ stock_analyzer_main.py 自身は sector_agents/*.py を import しない
#     → 動的ロードで自動検出するため循環参照なし
# ==============================================================

import subprocess, sys, os, json, time, shutil
from pathlib import Path
from datetime import datetime, timedelta
# momentum_factor（動的ロード）
# ← 直接importはせず、BLOCK X で動的ロードする

def _install():
    pkgs = [
        "pandas", "yfinance", "requests", "openpyxl",
        "matplotlib", "scipy", "gspread", "google-auth",
        "schedule", "feedparser", "streamlit", "plotly",
    ]
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "-q"] + pkgs,
        capture_output=True,
    )
_install()

import schedule
import pandas as pd

# add by gemini20260311
import logging
import warnings

# yfinance / streamlit のノイズログを抑制
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
logging.getLogger("streamlit").setLevel(logging.ERROR)
logging.getLogger("streamlit.runtime.caching.cache_data_api").setLevel(logging.ERROR)
logging.getLogger("streamlit.runtime.scriptrunner").setLevel(logging.ERROR)

# GitHub Actions（非UI環境）での streamlit WARNINGを抑制
warnings.filterwarnings(
    "ignore",
    message=".*No runtime found.*",
    category=UserWarning,
)
warnings.filterwarnings(
    "ignore",
    message=".*MemoryCacheStorageManager.*",
    category=UserWarning,
)
# add by gemini20260311

# ── config.py をロード（パス解決付き） ────────────────────────
def _load_config():
    try:
        import config
        return config
    except ImportError:
        import importlib.util
        for p in [
            "/content/config.py",
            str(Path(__file__).parent / "config.py"),
        ]:
            if Path(p).exists():
                spec = importlib.util.spec_from_file_location("config", p)
                mod  = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                sys.modules["config"] = mod
                return mod
        raise ImportError("config.py が見つかりません")

cfg = _load_config()


# ==============================================================
# BLOCK 1: フォルダ準備
# ==============================================================

def setup_dirs():
    """cfg.ALL_DIRS に定義された全フォルダを作成する"""
    for d in cfg.ALL_DIRS:
        d.mkdir(parents=True, exist_ok=True)
    print(f"✅ フォルダ準備完了: {cfg.DRIVE_BASE}")


# ==============================================================
# BLOCK 2: ログ管理
# ==============================================================
# 【v2.0 との差分】
#   ・log_path を cfg.LOG_DIR / cfg.LOG_FILENAME に変更
#   ・最大保持件数を cfg.LOG_MAX_ENTRIES から参照

class RunLogger:
    def __init__(self):
        self.log_path = cfg.LOG_DIR / cfg.LOG_FILENAME
        self._logs    = self._load()

    def _load(self):
        if self.log_path.exists():
            try:
                return json.loads(self.log_path.read_text(encoding="utf-8"))
            except Exception:
                return []
        return []

    def add(self, module: str, status: str, note: str = ""):
        entry = {
            "datetime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "module":   module,
            "status":   status,   # "success" / "error" / "skipped"
            "note":     note,
        }
        self._logs.append(entry)
        self._logs = self._logs[-cfg.LOG_MAX_ENTRIES:]
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self.log_path.write_text(
                json.dumps(self._logs, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass
        icon = "✅" if status == "success" else ("⏩" if status == "skipped" else "❌")
        print(f"  {icon} LOG [{status}] {module}: {note}")

    def tail(self, n: int = 5):
        print(f"\n  {'─'*52}")
        print(f"  実行ログ（直近{n}件）")
        print(f"  {'─'*52}")
        for e in self._logs[-n:]:
            icon = "✅" if e["status"] == "success" else (
                "⏩" if e["status"] == "skipped" else "❌"
            )
            print(f"  {icon} {e['datetime']} | {e['module']} | {e['note']}")


logger = RunLogger()


# ==============================================================
# BLOCK 3: マクロ指標取得・追い風/逆風判定
# ==============================================================
# 【v2.0 との差分】このブロック全体が新規追加

def fetch_macro_snapshot() -> dict:
    """
    yfinance で cfg.MACRO_TICKERS に定義されたマクロ指標を取得する。
    取得失敗時は None として辞書に格納（エラーで全体が止まらない設計）。
    """
    try:
        import yfinance as yf
    except ImportError:
        print("  ⚠️  yfinance が未インストールです")
        return {}

    snapshot = {}
    print("\n  📡 マクロ指標を取得中...")
    for key, ticker in cfg.MACRO_TICKERS.items():
        try:
            info  = yf.Ticker(ticker).info or {}
            price = info.get("regularMarketPrice") or info.get("currentPrice")
            snapshot[key] = float(price) if price else None
            status = f"{price:.3g}" if price else "N/A"
            print(f"    {key:10s}: {status}")
        except Exception as e:
            snapshot[key] = None
            print(f"    {key:10s}: 取得失敗 ({str(e)[:40]})")
    return snapshot

def evaluate_cross_sector_signals(results: dict) -> list:
    """
    CROSS_SECTOR_RULES を評価して横断産業シグナルのリストを返す。

    govデータ・macroスナップショット・bond_commodityの各値を参照し、
    発動条件を満たしたルールのみ返す。

    Returns:
        list of dict: [
          {
            "rule_id":   str,   # "machine_tool_orders_up"
            "label":     str,
            "lag_weeks": int,
            "signal":    dict,  # {"tailwind": [...], "headwind": [...]}
            "rationale": dict,
            "triggered_val": float,  # 実際の指標値
          }
        ]
    """
    import json
    active = []
    rules  = getattr(cfg, "CROSS_SECTOR_RULES", {})
    if not rules:
        return active

    # ── [1. シグナル判定処理] ───────────────────────
    # (先に対象データを取得してシグナルが発動したか判定し、activeに格納する)
    gov_data   = results.get("gov", {}) or {}
    macro_snap = results.get("macro_snapshot", {}) or {}
    bc_data    = results.get("bond_commodity", {}) or {}

    for rule_id, rule in rules.items():
        try:
            trigger = rule.get("trigger", {})
            source  = trigger.get("source", "macro")
            key     = trigger.get("key", "")
            op      = trigger.get("op", ">")
            val     = trigger.get("val", 0)

            # 対象ソースから値を取得
            if source == "gov":
                # gov_data_pipeline の結果は {key: {values:[...], change_pct:...}} の形式
                raw = gov_data.get(key)
                if isinstance(raw, dict):
                    # yoy変化率を優先
                    mval = raw.get("yoy_change") or raw.get("change_pct") or raw.get("latest")
                else:
                    mval = raw
            elif source == "macro":
                mval = macro_snap.get(key)
            elif source == "bond_commodity":
                mval = bc_data.get(key)
            else:
                mval = None

            if mval is None:
                continue  # データなし → スキップ（エラーにしない）

            mval_f = float(mval)

            # 条件判定
            fired = False
            if   op == ">":  fired = mval_f >  val
            elif op == ">=": fired = mval_f >= val
            elif op == "<":  fired = mval_f <  val
            elif op == "<=": fired = mval_f <= val

            if fired:
                active.append({
                    "rule_id":       rule_id,
                    "label":         rule.get("label", rule_id),
                    "lag_weeks":     rule.get("lag_weeks", 0),
                    "signal":        rule.get("signal", {}),
                    "rationale":     rule.get("rationale", {}),
                    "triggered_val": round(mval_f, 3),
                })

        except Exception:
            pass  # 1ルール失敗しても全体を止めない

    # ── [2. シグナル履歴保存・有効期限チェック] ─────────
    # (上で作った active リストに対して、履歴チェックと有効期限フィルタをかける)
    today_str  = datetime.now().strftime("%Y-%m-%d")
    hist_path  = Path(getattr(cfg, "DATA_DIR", "stock_data/data")) \
                 / "cross_sector_signal_history.json"

    # 履歴読み込み
    history = {}
    if hist_path.exists():
        try:
            history = json.loads(hist_path.read_text(encoding="utf-8"))
        except Exception:
            history = {}

    # 今回発動したシグナルを履歴に記録（新規 or 継続）
    for sig in active:
        rid = sig["rule_id"]
        if rid not in history or history[rid].get("first_fired") is None:
            history[rid] = {
                "first_fired": today_str,
                "last_fired":  today_str,
                "lag_weeks":   sig["lag_weeks"],
            }
        else:
            history[rid]["last_fired"] = today_str

    # 有効期限チェック: lag_weeks を超えたシグナルを除外
    # （「工作機械受注が先月高かった」を今月も使い続けないよう失効）
    valid_active = []
    for sig in active:
        rid   = sig["rule_id"]
        entry = history.get(rid, {})
        first = entry.get("first_fired", today_str)
        try:
            first_dt   = datetime.strptime(first, "%Y-%m-%d")
            elapsed_w  = (datetime.now() - first_dt).days / 7
            lag_weeks  = sig.get("lag_weeks", 4)
            if elapsed_w <= lag_weeks:
                sig["remaining_weeks"] = round(lag_weeks - elapsed_w, 1)
                valid_active.append(sig)
            else:
                # 有効期限切れ → 履歴から削除
                history.pop(rid, None)
                print(f"    ⏰ [R-1] シグナル失効: {sig['label']} "
                      f"（{elapsed_w:.0f}週経過 / 上限{lag_weeks}週）")
        except Exception:
            valid_active.append(sig)  # パース失敗は有効として継続

    # 履歴保存
    try:
        hist_path.parent.mkdir(parents=True, exist_ok=True)
        hist_path.write_text(
            json.dumps(history, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
    except Exception as e:
        print(f"    ⚠️  cross_sector_signal_history 保存失敗: {e}")

    # 最終的にフィルタリングされたリストを返す
    return valid_active

def evaluate_leading_indicators(results: dict) -> list:
    """
    LEADING_INDICATOR_RULES を評価して先行指標シグナルのリストを返す。
    evaluate_cross_sector_signals() と同形式で動作する。
    有効期限管理は cross_sector_signal_history.json を共有して使う。
    """
    active = []
    rules  = getattr(cfg, "LEADING_INDICATOR_RULES", {})
    if not rules:
        return active

    gov_data   = results.get("gov", {}) or {}
    macro_snap = results.get("macro_snapshot", {}) or {}

    for rule_id, rule in rules.items():
        try:
            trigger = rule.get("trigger", {})
            source  = trigger.get("source", "gov")
            key     = trigger.get("key", "")
            op      = trigger.get("op", ">")
            val     = trigger.get("val", 0)

            if source == "gov":
                raw  = gov_data.get(key)
                mval = (raw.get("yoy_change") or raw.get("change_pct") or raw.get("latest")
                        if isinstance(raw, dict) else raw)
            else:
                mval = macro_snap.get(key)

            if mval is None:
                continue

            mval_f = float(mval)
            fired  = False
            if   op == ">":  fired = mval_f >  val
            elif op == ">=": fired = mval_f >= val
            elif op == "<":  fired = mval_f <  val
            elif op == "<=": fired = mval_f <= val

            if fired:
                active.append({
                    "rule_id":       rule_id,
                    "label":         rule.get("label", rule_id),
                    "lag_weeks":     rule.get("lag_weeks", 0),
                    "signal":        rule.get("signal", {}),
                    "rationale":     rule.get("rationale", {}),
                    "triggered_val": round(mval_f, 3),
                    "is_leading":    True,  # cross_sector との区別用
                })
        except Exception:
            pass

    return active

def _calc_debt_cycle_score(cfg, results: dict) -> dict:
    """
    [D-2] 長期債務サイクル指標を算出する（ダリオ理論）。

    民間信用/GDP + HYスプレッド + VIX を統合して
    debt_cycle_score（0〜100）を算出する。

    score > 70: デレバレッジ局面 → 全業種に-10pt・現金/債券推奨
    score < 30: レバレッジ拡大初期 → 株式に+5pt
    30〜70:     中立

    Returns:
        {
          "debt_cycle_score": int,
          "label":            str,
          "hy_spread":        float | None,
          "credit_gdp":       float | None,
          "sector_adj":       float,  # 全業種への一律補正量
          "skipped":          bool,
        }
    """
    if not getattr(cfg, "ENABLE_DEBT_CYCLE", True):
        return {"skipped": True, "debt_cycle_score": 50, "sector_adj": 0.0}

    fred_key = getattr(cfg, "POLICY_FRED_API_KEY", "") or ""
    score    = 50.0   # 初期値は中立
    hy_spread   = None
    credit_gdp  = None

    # ── ① FRED から HY スプレッド取得 ──────────────────────
    if fred_key:
        try:
            import requests, time as _time
            def _fred_latest(series_id: str) -> float | None:
                r = requests.get(
                    "https://api.stlouisfed.org/fred/series/observations",
                    params={
                        "series_id":   series_id,
                        "api_key":     fred_key,
                        "file_type":   "json",
                        "sort_order":  "desc",
                        "limit":       5,
                    },
                    timeout=10,
                )
                if r.status_code != 200:
                    return None
                obs = [o for o in r.json().get("observations", [])
                       if o.get("value", ".") != "."]
                return float(obs[0]["value"]) if obs else None

            series = getattr(cfg, "DEBT_CYCLE_FRED_SERIES", {})
            hy_spread  = _fred_latest(series.get("hy_spread", "BAMLH0A0HYM2"))
            credit_gdp = _fred_latest(series.get("credit_gdp", "CRDQJAPABIS"))
            _time.sleep(0.2)
        except Exception as _e:
            print(f"    ⚠️  FRED(D-2)取得失敗: {str(_e)[:60]}")

    # ── ② フォールバック: bond_commodity / macro_snapshot から取得 ──
    if hy_spread is None:
        bc = results.get("bond_commodity", {})
        # VIX を HY スプレッドの代理変数として使用
        snap = results.get("macro_snapshot", {})
        vix = snap.get("vix")
        if vix is not None:
            # VIX > 30 → HY スプレッド高と推定
            hy_spread = round(float(vix) * 0.2, 2)  # 近似換算

    # ── ③ スコア算出 ─────────────────────────────────────────
    # HY スプレッド（単位: %）
    if hy_spread is not None:
        # HY スプレッド: 通常2〜4% / 危機時8〜12%
        if   hy_spread > 8.0:  score += 30.0   # 信用収縮・デレバレッジ警戒
        elif hy_spread > 6.0:  score += 20.0
        elif hy_spread > 4.5:  score += 10.0
        elif hy_spread < 3.0:  score -= 15.0   # 信用拡大・リスクオン
        elif hy_spread < 2.5:  score -= 25.0

    # 民間信用/GDP（単位: %）
    if credit_gdp is not None:
        # 日本は 180〜200% 前後が通常水準
        if   credit_gdp > 220.0:  score += 20.0   # 過剰レバレッジ
        elif credit_gdp > 200.0:  score += 10.0
        elif credit_gdp < 160.0:  score -= 10.0   # レバレッジ低い = 拡大余地

    score = int(round(max(0, min(100, score))))

    # ── ④ ラベルと業種補正量 ────────────────────────────────
    delever_thresh = getattr(cfg, "DEBT_CYCLE_DELEVER_THRESH", 70)
    expand_thresh  = getattr(cfg, "DEBT_CYCLE_EXPAND_THRESH",  30)

    if score >= delever_thresh:
        label      = "🔴 デレバレッジ局面（信用収縮・守りへ）"
        sector_adj = -10.0
    elif score <= expand_thresh:
        label      = "🟢 レバレッジ拡大初期（リスクテイク好機）"
        sector_adj = +5.0
    else:
        label      = "🟡 中立（債務サイクル中間）"
        sector_adj = 0.0

    print(f"    💳 [D-2] 債務サイクルスコア: {score}/100 → {label}")
    if hy_spread is not None:
        print(f"       HYスプレッド: {hy_spread:.2f}% "
              f"/ 民間信用GDP比: {credit_gdp:.1f}%" if credit_gdp else
              f"       HYスプレッド: {hy_spread:.2f}%")

    return {
        "debt_cycle_score": score,
        "label":            label,
        "hy_spread":        hy_spread,
        "credit_gdp":       credit_gdp,
        "sector_adj":       sector_adj,
        "skipped":          False,
    }

def _calc_sector_tier(sector_results: dict, results: dict) -> dict:
    """
    sector_results の composite_score と momentum を元に
    各業種を TOP / MID / BOT の3段階に分類する。

    Returns:
        {sector_code: "TOP" / "MID" / "BOT"}
    """
    top_thresh  = getattr(cfg, "SECTOR_TIER_TOP_THRESHOLD",  65.0)
    mid_thresh  = getattr(cfg, "SECTOR_TIER_MID_THRESHOLD",  45.0)
    up_slope    = getattr(cfg, "SECTOR_TIER_UPGRADE_SLOPE",   2.5)
    down_slope  = getattr(cfg, "SECTOR_TIER_DOWNGRADE_SLOPE", -2.5)
    crisis_max  = getattr(cfg, "SECTOR_TIER_CRISIS_MAX_TOP",  3)

    # 市場フェーズ取得（inference_engine が未実行の場合は "neutral"）
    market_phase = results.get("economic_cycle", {}).get("phase", "B")
    is_crisis    = results.get("inference", {}).get("phase_code") in ("crisis",)

    # momentumスロープ取得（momentum_factor の結果）
    momentum_res = results.get("momentum", {})

    tier = {}
    for code, res in sector_results.items():
        if not isinstance(res, dict):
            tier[code] = "BOT"
            continue

        score = float(res.get("composite_score", 0))

        # ── 基本分類 ──────────────────────────────────────
        if score >= top_thresh:
            t = "TOP"
        elif score >= mid_thresh:
            t = "MID"
        else:
            t = "BOT"

        # ── momentum による格上げ/格下げ ─────────────────
        slope = None
        if isinstance(momentum_res, dict):
            mom_entry = momentum_res.get(code)
            if isinstance(mom_entry, dict):
                slope = mom_entry.get("slope_per_week")

        if slope is not None:
            if slope >= up_slope and t == "MID":
                t = "TOP"   # MID→TOP 格上げ
            elif slope <= down_slope and t == "TOP":
                t = "MID"   # TOP→MID 格下げ

        tier[code] = t

    # ── 危機モード: TOP_TIER を上位 crisis_max 業種に制限 ──
    if is_crisis:
        top_codes = sorted(
            [c for c, t in tier.items() if t == "TOP"],
            key=lambda c: sector_results.get(c, {}).get("composite_score", 0),
            reverse=True,
        )
        for c in top_codes[crisis_max:]:
            tier[c] = "MID"   # crisis_max 超の TOP は MID に降格

    # ── 象限C（スタグフレーション）: 全体を1段階引き下げ ──
    if market_phase == "C":
        new_tier = {}
        for c, t in tier.items():
            if t == "TOP":
                new_tier[c] = "MID"
            elif t == "MID":
                new_tier[c] = "BOT"
            else:
                new_tier[c] = "BOT"
        tier = new_tier

    return tier

def evaluate_macro_signals(snapshot: dict, weights: dict = None) -> list:
    """
    マクロスナップショットから cfg.MACRO_RULES に基づき
    現在アクティブなマクロ要因とシグナルを評価する。

    ★ ルールの変更は config.py の MACRO_RULES のみを編集すればよい。
      このロジックは変更不要。

    Returns:
        list of dict: [
          {
            "factor":         str,  # "interest_rate_rise"
            "label":          str,  # "金利上昇"
            "trigger_value":  str,  # "米10年債: 4.50%"
            "sector_signals": dict, # {"1028": "tailwind", "1032": "headwind", ...}
            "rationale":      dict, # {"1028": "預貸金利ザヤ拡大..."}
          }
        ]
    """
    active_signals = []
    weights = weights or {}   # None ガード。rule_optimizer 未実行時は空辞書で動作
    usdjpy = snapshot.get("usdjpy")
    us10y  = snapshot.get("us10y")
    oil    = snapshot.get("oil_wti")
    vix    = snapshot.get("vix")

    # ── 金利判定 ─────────────────────────────────────────────
    if us10y is not None:
        factor = "interest_rate_rise" if us10y > 4.0 else "interest_rate_fall"
        rule   = cfg.MACRO_RULES.get(factor, {})
        if rule:
            active_signals.append({
                "factor":         factor,
                "label":          rule["label"],
                "trigger_value":  f"米10年債: {us10y:.2f}%",
                "sector_signals": _build_sector_signals(rule),
                "rationale":      rule.get("rationale", {}),
                "weight":         weights.get(factor, 1.0),
            })

    # ── 為替判定 ─────────────────────────────────────────────
    if usdjpy is not None:
        factor = "yen_depreciation" if usdjpy > 140 else "yen_appreciation"
        rule   = cfg.MACRO_RULES.get(factor, {})
        if rule:
            active_signals.append({
                "factor":         factor,
                "label":          rule["label"],
                "trigger_value":  f"USD/JPY: {usdjpy:.2f}",
                "sector_signals": _build_sector_signals(rule),
                "rationale":      rule.get("rationale", {}),
                "weight":         weights.get(factor, 1.0),
            })

    # ── 原油判定 ─────────────────────────────────────────────
    if oil is not None and oil > 80:
        rule = cfg.MACRO_RULES.get("oil_price_rise", {})
        if rule:
            active_signals.append({
                "factor":         "oil_price_rise",
                "label":          rule["label"],
                "trigger_value":  f"WTI: ${oil:.1f}",
                "sector_signals": _build_sector_signals(rule),
                "rationale":      rule.get("rationale", {}),
                "weight":         weights.get(factor, 1.0),
            })

    # ── VIX 判定 ─────────────────────────────────────────────
    if vix is not None and vix > 25:
        rule = cfg.MACRO_RULES.get("us_recession_risk", {})
        if rule:
            active_signals.append({
                "factor":         "us_recession_risk",
                "label":          rule["label"],
                "trigger_value":  f"VIX: {vix:.1f}",
                "sector_signals": _build_sector_signals(rule),
                "rationale":      rule.get("rationale", {}),
                "weight":         weights.get(factor, 1.0),
            })
    # ── [GAP-7] 各シグナルに delta 情報を付与 ──────────────────
    delta_alerts = snapshot.get("_delta_alerts", [])
    alert_keys   = {a["key"] for a in delta_alerts}

    # macro_snapshot の対応キーで急変フラグを設定
    FACTOR_SNAPSHOT_KEY = {
        "interest_rate_rise":  "us10y",
        "interest_rate_fall":  "us10y",
        "yen_depreciation":    "usdjpy",
        "yen_appreciation":    "usdjpy",
        "oil_price_rise":      "oil_wti",
        "us_recession_risk":   "vix",
        "high_inflation":      "us10y",
        "geopolitical_risk":   "vix",
    }
    for sig in active_signals:
        factor   = sig.get("factor", "")
        snap_key = FACTOR_SNAPSHOT_KEY.get(factor)
        sig["delta"]       = snapshot.get(f"{snap_key}_delta")     if snap_key else None
        sig["delta_pct"]   = snapshot.get(f"{snap_key}_delta_pct") if snap_key else None
        sig["delta_alert"] = bool(snap_key and snap_key in alert_keys)
        if sig["delta"] is not None:
            sign = "+" if sig["delta"] >= 0 else ""
            sig["delta_str"] = f"{snap_key}: {sign}{sig['delta']:.3f}"

    return active_signals


def _build_sector_signals(rule: dict) -> dict:
    """
    rule["signal"] 辞書から {sector_code: signal_type} の
    フラットな辞書を生成する（UI 表示用）。
    """
    result = {}
    for signal_type, codes in rule.get("signal", {}).items():
        for code in codes:
            result[code] = signal_type
    return result


def get_sector_macro_icon(sector_code: str, active_signals: list) -> str:
    """
    業種コードに対して最終的なアイコンを返す。
    複数シグナルが競合する場合は headwind を優先。
    """
    signals = [
        s["sector_signals"].get(sector_code)
        for s in active_signals
        if s["sector_signals"].get(sector_code)
    ]
    if "headwind" in signals:
        return "🔴"
    if "tailwind" in signals:
        return "💚"
    if "watch" in signals:
        return "🟡"
    return "⚪"


# ==============================================================
# BLOCK 4: セクターエージェント 動的ロードインターフェース
# ==============================================================
# 【v2.0 との差分】このブロック全体が新規追加（Step 2 以降で本格化）

def load_sector_agent(sector_code: str):
    """
    cfg.AGENTS_DIR/{sector_code}_agent.py を動的ロードして返す。
    ファイルが存在しない場合は None を返す（エラーにしない）。

    Step 2 以降でエージェントを追加するときは:
      sector_agents/1028_agent.py に run_analysis() を実装するだけ。
    ★ このメイン側（stock_analyzer_main.py）は変更不要。
    """
    agent_path = cfg.AGENTS_DIR / f"{sector_code}_agent.py"
    if not agent_path.exists():
        return None
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            f"agent_{sector_code}", str(agent_path)
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception as e:
        print(f"  ⚠️  エージェントロード失敗 ({sector_code}): {e}")
        return None

# ==============================================================
# BLOCK X: スコア履歴・スクリーナー履歴 蓄積
# ==============================================================

def _append_score_history(cfg, sector_results: dict) -> None:
    """
    業種別composite_scoreを日次で蓄積する。
    保存先: DATA_DIR/score_history.json
    形式:   {"YYYY-MM-DD": {"1001": {"score": 72.0, "risk": "low"}, ...}}
    保持:   直近180日分（古い日付から自動削除）
    """
    import json
    try:
        path = Path(cfg.DATA_DIR) / "score_history.json"
        today = datetime.now().strftime("%Y-%m-%d")

        # 既存データ読み込み
        history = {}
        if path.exists():
            try:
                history = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                history = {}

        # 今日のスコアを追記
        today_data = {}
        for code, r in sector_results.items():
            if not isinstance(r, dict):
                continue
            score = r.get("composite_score")
            risk  = r.get("risk_level", "N/A")
            if score is not None:
                today_data[code] = {
                    "score": round(float(score), 1),
                    "risk":  risk,
                }
        if today_data:
            history[today] = today_data

        # 180日分だけ保持（古いものを削除）
        keys = sorted(history.keys())
        if len(keys) > 180:
            for old_key in keys[:-180]:
                del history[old_key]

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(history, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        print(f"  ✅ score_history: {len(today_data)}業種を記録（累計{len(history)}日分）")
    except Exception as e:
        print(f"  ⚠️  score_history 保存失敗: {str(e)[:60]}")

def run_sector_analysis(sector_code: str, macro_snapshot: dict) -> dict:
    """
    指定業種の分析を実行する。
    エージェントが実装済みの場合はそちらを呼び出す。
    未実装の場合はデフォルト（config ベースのシグナル評価のみ）を返す。

    Returns:
        dict: cfg.SECTOR_AGENT_RESULT_SCHEMA に準拠した辞書
    """
    sector_info = cfg.JAPAN_SECTORS.get(sector_code, {})
    agent       = load_sector_agent(sector_code)

    if agent and hasattr(agent, "run_analysis"):
        try:
            return agent.run_analysis(sector_code, cfg,
                                      macro_snapshot=macro_snapshot)
        except Exception as e:
            print(f"  ⚠️  エージェント実行失敗 ({sector_code}): {e}")

    # デフォルト: config ベースのシグナル評価のみ
    active_signals = evaluate_macro_signals(macro_snapshot)
    sector_signals = [
        {
            "factor": s["factor"],
            "label":  s["label"],
            "signal": s["sector_signals"].get(sector_code, "neutral"),
            "trigger": s["trigger_value"],
        }
        for s in active_signals
        if s["sector_signals"].get(sector_code)
    ]

    return {
        "sector_code":     sector_code,
        "sector_name_ja":  sector_info.get("name_ja", sector_code),
        "analysis_date":   datetime.now().strftime("%Y-%m-%d"),
        "composite_score": 0.0,   # BUG-01修正: エージェント未実装時のデフォルト値
        "macro_signals":   sector_signals,
        "top_companies":   [],
        "arxiv_papers":    [],
        "insight_text":    "",
        "risk_level":      "N/A",
    }


# ==============================================================
# BLOCK 5: 各モジュール実行ラッパー
# ==============================================================
# 【v2.0 との差分】
#   ・_load_local_module() 共通処理を抽出（重複コードを削減）
#   ・config 参照に統一

def _load_local_module(name: str, local_path: str, drive_path: Path):
    """モジュールをロードする（GitHub Actions / Colab 両対応）"""
    import importlib.util

    # 候補パスを順番に探す
    candidates = [
        Path(local_path),                        # /content/xxx.py（Colab）
        drive_path,                               # Google Drive パス
        Path(__file__).parent / f"{name}.py",    # リポジトリ直下（GitHub Actions）
    ]

    found = None
    for p in candidates:
        if p.exists():
            found = p
            break

    if found is None:
        return None

    try:
        spec = importlib.util.spec_from_file_location(name, str(found))
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception as e:
        print(f"  ❌ {name} ロード失敗: {e}")
        return None

# ==============================================================
# [問題B] composite_score 補正量の一元管理関数
# ==============================================================

def _apply_capped_adjustment(
    sector_results: dict,
    adjustments:    dict,
    adj_key:        str,
    cap_config_key: str = "SCORE_MAX_TOTAL_ADJ",
    label:          str = "",
) -> dict:
    """
    sector_results の composite_score に補正量を適用する。
    上限（cap_config_key で指定された config 値）を超えた場合は
    比例縮小して適用し、クランプ箇所と理由を記録する。

    Args:
        sector_results : {"sector_code": {"composite_score": float, ...}}
        adjustments    : {"sector_code": delta_pt}
        adj_key        : sector_results に書き込むキー名（例: "macro_adj"）
        cap_config_key : config.py の補正上限変数名
        label          : ログ用ラベル

    Returns:
        updated sector_results（in-place 更新済み）
    """
    cap = float(getattr(cfg, cap_config_key, 30.0))
    global_cap = float(getattr(cfg, "SCORE_MAX_TOTAL_ADJ", 30.0))

    for code, delta in adjustments.items():
        if code not in sector_results:
            continue
        res = sector_results[code]
        if not isinstance(res, dict):
            continue

        old_score = float(res.get("composite_score") or 0.0)

        # カテゴリ上限チェック
        capped_delta = delta
        if abs(delta) > cap:
            capped_delta = cap if delta > 0 else -cap
            res[f"{adj_key}_capped"] = True
            res[f"{adj_key}_original"] = delta

        # 全補正累積の上限チェック（total_adj は今回の分を含む）
        existing_total = float(res.get("_total_adj_so_far", 0.0))
        new_total = existing_total + capped_delta
        if new_total > global_cap:
            capped_delta = global_cap - existing_total
        elif new_total < -global_cap:
            capped_delta = -global_cap - existing_total

        new_score = round(max(0.0, min(100.0, old_score + capped_delta)), 1)
        res["composite_score"]         = new_score
        res[adj_key]                   = round(capped_delta, 2)
        res["_total_adj_so_far"]       = round(existing_total + capped_delta, 2)

    return sector_results

def run_gov_pipeline():
    """gov_data_pipeline.py を実行する"""
    print(f"\n{'='*60}")
    print("  📊 政府データパイプライン 実行開始")
    print(f"{'='*60}")

    mod = _load_local_module(
        "gov_data_pipeline",
        "/content/gov_data_pipeline.py",
        cfg.DRIVE_BASE / "gov_data_pipeline.py",
    )
    if not mod:
        msg = "gov_data_pipeline.py が見つかりません"
        print(f"  ❌ {msg}")
        logger.add("gov_pipeline", "error", msg)
        return False

    try:
        result = mod.main(cfg=cfg)
        fetched = result.get("stats_fetched", 0) if isinstance(result, dict) else 0
        total   = result.get("stats_total",   0) if isinstance(result, dict) else 0
        logger.add("gov_pipeline", "success", f"政府統計取得完了: {fetched}/{total}")
        return result if isinstance(result, dict) else True
    except Exception as e:
        msg = str(e)[:120]
        print(f"  ❌ 実行エラー: {msg}")
        logger.add("gov_pipeline", "error", msg)
        return False


def run_arxiv_monitor():
    """arxiv_monitor.py を実行し、結果 dict を返す（etf_tracker との連携用）"""
    print(f"\n{'='*60}")
    print("  📚 ArXiv 論文監視 実行開始")
    print(f"{'='*60}")

    mod = _load_local_module(
        "arxiv_monitor",
        "/content/arxiv_monitor.py",
        cfg.DRIVE_BASE / "arxiv_monitor.py",
    )
    if not mod:
        msg = "arxiv_monitor.py が見つかりません"
        print(f"  ❌ {msg}")
        logger.add("arxiv_monitor", "skipped", msg)
        return {}   # ★ False → {} に変更（etf_tracker が dict チェックするため）

    try:
        arxiv_result = mod.run_monitor()

        # 戻り値が dict でない場合（旧バージョン互換）は空 dict に変換
        if not isinstance(arxiv_result, dict):
            arxiv_result = {}

        n        = arxiv_result.get("investment_ideas_found", 0)
        n_papers = arxiv_result.get("new_papers", 0)
        logger.add("arxiv_monitor", "success",
                   f"新着{n_papers}件, 候補{n}件")
        return arxiv_result   # ★ dict をそのまま返す

    except Exception as e:
        msg = str(e)[:120]
        print(f"  ❌ ArXiv 監視エラー: {msg}")
        logger.add("arxiv_monitor", "error", msg)
        return {}   # ★ False → {} に変更


def _save_macro_report(snapshot: dict, signals: list):
    """マクロ分析レポートを Markdown で保存する（新規追加）"""
    cfg.MACRO_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    path  = cfg.MACRO_DIR / f"macro_report_{today}.md"

    md  = f"# マクロ経済 分析レポート\n\n"
    md += f"**更新**: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
    md += "## 現在のマクロ指標\n\n| 指標 | 値 |\n|------|-----|\n"

    labels = {
        "usdjpy":  "USD/JPY",
        "us10y":   "米10年債利回り(%)",
        "oil_wti": "WTI原油($)",
        "gold":    "金($)",
        "vix":     "VIX",
        "sp500":   "S&P500",
        "nikkei":  "日経平均",
    }
    for k, v in snapshot.items():
        label = labels.get(k, k)
        val   = f"{v:.3g}" if v is not None else "N/A"
        md += f"| {label} | {val} |\n"

    md += "\n## アクティブなマクロシグナル\n\n"
    if not signals:
        md += "*現在特筆すべきシグナルはありません*\n\n"
    else:
        for s in signals:
            md += f"### {s['label']}（{s['trigger_value']}）\n\n"
            md += "| 業種 | 判定 | 理由 |\n|------|------|------|\n"
            for code, sig in s["sector_signals"].items():
                name = cfg.JAPAN_SECTORS.get(code, {}).get("name_ja", code)
                icon = {
                    "tailwind": "💚 追い風",
                    "headwind": "🔴 逆風",
                    "watch":    "🟡 注意",
                }.get(sig, "⚪")
                reason = s["rationale"].get(code, "")
                md += f"| {name} | {icon} | {reason} |\n"
            md += "\n"

    path.write_text(md, encoding="utf-8")
    print(f"  ✅ マクロレポート保存: {path}")

def _load_prev_macro_snapshot(cfg) -> dict:
    """前日の macro_snapshot を JSON キャッシュから読み込む。"""
    try:
        p = Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / "macro_snapshot_prev.json"
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}

# GAP-7 の急変閾値（指標ごとに設定）
_DELTA_THRESHOLDS = {
    "usdjpy":  3.0,    # 3円以上の変化
    "us10y":   0.2,    # 0.2%以上の変化
    "oil_wti": 5.0,    # 5ドル以上の変化
    "vix":     5.0,    # VIX 5pt以上の変化
    "sp500":   100.0,  # S&P500 100pt以上の変化
    "gold":    30.0,   # 金 30ドル以上の変化
}


def _add_macro_deltas(snapshot: dict, prev_snapshot: dict) -> dict:
    """
    [GAP-7] 今回の macro_snapshot に前回比の delta と急変フラグを付与する。

    Args:
        snapshot      : 今回の fetch_macro_snapshot() 戻り値
        prev_snapshot : 前日の macro_snapshot（キャッシュ）

    Returns:
        delta フィールドを追加した snapshot dict
        各キーに対して:
          "{key}_delta":       float  前回差分（今回 - 前回）
          "{key}_delta_pct":   float  前回比変化率%
          "{key}_alert":       bool   急変フラグ
        "_delta_alerts": list  急変したキーのリスト
    """
    result  = dict(snapshot)
    alerts  = []

    for key, threshold in _DELTA_THRESHOLDS.items():
        cur  = snapshot.get(key)
        prev = prev_snapshot.get(key)
        if cur is None or prev is None:
            continue
        try:
            cur_f   = float(cur)
            prev_f  = float(prev)
            delta   = cur_f - prev_f
            delta_p = (delta / prev_f * 100) if prev_f != 0 else 0.0
            is_alert = abs(delta) >= threshold

            result[f"{key}_delta"]     = round(delta, 4)
            result[f"{key}_delta_pct"] = round(delta_p, 2)
            result[f"{key}_alert"]     = is_alert

            if is_alert:
                sign = "+" if delta > 0 else ""
                alerts.append({
                    "key":       key,
                    "delta":     round(delta, 3),
                    "delta_str": f"{key}: {prev_f:.2f}→{cur_f:.2f}（{sign}{delta:.2f}）",
                })
        except (TypeError, ValueError):
            pass

    result["_delta_alerts"] = alerts
    return result

def _save_macro_snapshot_cache(cfg, snapshot: dict) -> None:
    """今回の macro_snapshot を前日値として保存する。"""
    try:
        p = Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / "macro_snapshot_prev.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        data = {**snapshot, "_cached_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"    ⚠️  macro_snapshot_prev 保存失敗: {e}")

# ==============================================================
# BLOCK 6: 全タスク実行
# ==============================================================

def run_all_tasks(mode: str = "all") -> dict:
    """
    全モジュールを実行する。
    mode="all"     : 全モジュール実行（デフォルト・手動実行）
    mode="daily"   : DAILY_MODULES のみ実行
    mode="weekly"  : DAILY_MODULES + WEEKLY_MODULES を実行
    """
    daily_mods  = getattr(cfg, "DAILY_MODULES",  [])
    weekly_mods = getattr(cfg, "WEEKLY_MODULES", [])

    def _in_mode(key: str) -> bool:
        """このモジュールを現在の mode で実行すべきか判定する"""
        if mode == "all":
            return True
        if mode == "daily":
            return key in daily_mods
        if mode == "weekly":
            return key in daily_mods or key in weekly_mods
        return True

    start = datetime.now()
    print(f"\n{'='*60}")
    print(f"  🚀 ストックアナライザー v3.0 実行開始 [{mode.upper()} モード]")
    print(f"  {start.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    results = {}
    # 下流モジュールが参照するため ENABLE_SECTOR/ENABLE_MACRO=False 時も定義しておく
    macro_snapshot_for_sector = {}
    sector_results            = {}

   # ── [④] キャッシュ鮮度管理 ──────────────────────────────────
    if getattr(cfg, "ENABLE_CACHE_FRESHNESS", True):
        try:
            _cfm = _load_local_module(
                "cache_freshness_manager",
                "/content/cache_freshness_manager.py",
                cfg.DRIVE_BASE / "cache_freshness_manager.py",
            )
            if _cfm:
                cfm_result = _cfm.run_cache_freshness_manager(cfg, results)
                results["freshness"] = cfm_result
                # 強制再取得が必要なキャッシュをログに記録
                if cfm_result.get("refresh_caches"):
                    logger.add(
                        "cache_freshness", "alert",
                        f"強制再取得推奨: "
                        f"{', '.join(cfm_result['refresh_caches'][:5])}",
                    )
        except Exception as _cfme:
            print(f"  ⚠️  キャッシュ鮮度管理エラー（スキップ）: {_cfme}")

    if cfg.ENABLE_GOV_PIPELINE and _in_mode("gov"):
        results["gov"] = run_gov_pipeline()
    elif not _in_mode("gov"):
        print("  ⏩ 政府データ: スキップ（週次モジュール）")
    else:
        print("  ⏩ 政府データパイプライン: スキップ（config.ENABLE_GOV_PIPELINE=False）")

    if cfg.ENABLE_ARXIV and _in_mode("arxiv"):
        results["arxiv"] = run_arxiv_monitor()
    elif not _in_mode("arxiv"):
        print("  ⏩ ArXiv 監視: スキップ（週次モジュール）")
    else:
        print("  ⏩ ArXiv 監視: スキップ（config.ENABLE_ARXIV=False）")

    if cfg.ENABLE_MACRO and _in_mode("macro"):
        print("\n  📈 マクロ分析を実行中...")
        snapshot      = fetch_macro_snapshot()
        # ── [GAP-7] 前回スナップショットとの差分を計算 ─────────
        prev_snapshot = _load_prev_macro_snapshot(cfg)
        snapshot_with_delta = _add_macro_deltas(snapshot, prev_snapshot)

        signals = evaluate_macro_signals(
            snapshot_with_delta,
            weights=results.get("_macro_weights") or {}
        )
        _save_macro_report(snapshot_with_delta, signals)
        _save_macro_snapshot_cache(cfg, snapshot)   # 今回値を次回の前日値として保存

        results["macro"]          = True
        results["macro_snapshot"] = snapshot_with_delta
        results["macro_signals"]  = signals
        results["macro_delta_alerts"] = [
            s for s in signals if s.get("delta_alert")
        ]
        if results["macro_delta_alerts"]:
            print(f"    ⚡ [GAP-7] マクロ急変検出: "
                  f"{len(results['macro_delta_alerts'])}件")
            for s in results["macro_delta_alerts"][:3]:
                print(f"       {s['label']}: {s.get('delta_str','')}")

        # ── [R-1] 横断産業シグナル評価 ──────────────────────────
        cross_signals = evaluate_cross_sector_signals(results)
        results["cross_sector_signals"] = cross_signals
        # ── [問題A] lag_weeks 有効期限管理 ──────────────────────
        try:
            import json as _json
            from datetime import date as _dt, timedelta as _td
            _hist_path = (
                Path(getattr(cfg, "DATA_DIR", "stock_data/data"))
                / "cross_sector_signal_history.json"
            )

            # 既存の発動履歴を読み込む
            _hist = {}
            if _hist_path.exists():
                try:
                    _hist = _json.loads(_hist_path.read_text(encoding="utf-8"))
                except Exception:
                    pass

            today_str = _dt.today().strftime("%Y-%m-%d")
            active_signals = []

            for sig in results.get("cross_sector_signals", []):
                rule_id    = sig.get("rule_id", "")
                lag_weeks  = (
                    getattr(cfg, "CROSS_SECTOR_RULES", {})
                    .get(rule_id, {})
                    .get("lag_weeks", 4)
                )
                # 初回発動日を記録
                if rule_id not in _hist:
                    _hist[rule_id] = {"first_fired": today_str, "last_fired": today_str}
                else:
                    _hist[rule_id]["last_fired"] = today_str

                # 有効期限チェック（lag_weeks 週を超えたら失効）
                first_fired = _hist[rule_id].get("first_fired", today_str)
                try:
                    first_dt  = _dt.fromisoformat(first_fired)
                    age_weeks = (_dt.today() - first_dt).days / 7
                except Exception:
                    age_weeks = 0

                if age_weeks > lag_weeks:
                    sig["expired"]      = True
                    sig["age_weeks"]    = round(age_weeks, 1)
                    sig["expire_reason"] = (
                        f"lag_weeks={lag_weeks}を超過（{age_weeks:.1f}週経過）"
                    )
                    print(
                        f"    ⏰ [問題A] シグナル失効: {rule_id} "
                        f"（{age_weeks:.1f}週 > lag={lag_weeks}週）"
                    )
                else:
                    sig["expired"]   = False
                    sig["age_weeks"] = round(age_weeks, 1)
                    active_signals.append(sig)

            # 失効シグナルを除外して上書き
            expired_count = len(results["cross_sector_signals"]) - len(active_signals)
            if expired_count > 0:
                results["cross_sector_signals"] = active_signals
                print(f"    🗑️  [問題A] 失効シグナル除外: {expired_count}件")

            # 2年分保持して発動履歴を保存
            cutoff_2y = (_dt.today() - _td(days=730)).strftime("%Y-%m-%d")
            _hist = {
                k: v for k, v in _hist.items()
                if v.get("last_fired", today_str) >= cutoff_2y
            }
            _hist_path.parent.mkdir(parents=True, exist_ok=True)
            _hist_path.write_text(
                _json.dumps(_hist, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        except Exception as _lagex:
            print(f"  ⚠️  [問題A] lag_weeks チェックエラー: {_lagex}")

        # ── [GAP-8] シグナル状態を signal_history.json に記録 ──────
        if getattr(cfg, "ENABLE_ALPHA_ATTRIBUTION", True):
            try:
                _aa = _load_local_module(
                    "alpha_attributer",
                    "/content/alpha_attributer.py",
                    cfg.DRIVE_BASE / "alpha_attributer.py",
                )
                if _aa:
                    _aa.record_signal_state(cfg, results)
            except Exception as _ae:
                pass  # 記録失敗はメイン処理を止めない

        # ── [R-2] 先行指標シグナル評価 ──────────────────────────
        leading_signals = evaluate_leading_indicators(results)
        results["leading_indicator_signals"] = leading_signals
        if leading_signals:
            print(f"    📡 先行指標シグナル: {len(leading_signals)}件発動")
            for ls in leading_signals[:2]:
                print(f"       {ls['label'][:40]} "
                      f"(先行{ls['lag_weeks']}週)")
        if cross_signals:
            print(f"    🔗 横断産業シグナル: {len(cross_signals)}件発動")
            for cs in cross_signals[:3]:
                tw = len(cs["signal"].get("tailwind", []))
                hw = len(cs["signal"].get("headwind", []))
                print(f"       {cs['label']} "
                      f"(追い風{tw}業種/逆風{hw}業種 "
                      f"/ 先行{cs['lag_weeks']}週)")
        else:
            print("    🔗 横断産業シグナル: 発動なし")
    else:
        print("  ⏩ マクロ分析: スキップ（config.ENABLE_MACRO=False / Step 2 で有効化）")

# ── 経済サイクルフェーズ判定（D-1 / ダリオ4象限）────────────
    if getattr(cfg, "ENABLE_ECONOMIC_CYCLE", True) and _in_mode("economic_cycle"):
        try:
            _ec = _load_local_module(
                "economic_cycle_detector",
                "/content/economic_cycle_detector.py",
                cfg.DRIVE_BASE / "economic_cycle_detector.py",
            )
            if _ec:
                ec_result = _ec.run_economic_cycle_detector(cfg, results)
                results["economic_cycle"] = ec_result

                # ── [RESEARCH-1] HMMレジーム検知 ────────────────────────
                if getattr(cfg, "ENABLE_REGIME_DETECTOR", False):
                    try:
                        _rd = _load_local_module(
                            "regime_detector",
                            "/content/regime_detector.py",
                            cfg.DRIVE_BASE / "regime_detector.py",
                        )
                        if _rd:
                            rd_result = _rd.run_regime_detector(cfg, results)
                            results["regime"] = rd_result

                            # D-1の confidence を補正
                            if not rd_result.get("skipped"):
                                adj = rd_result.get("d1_confidence_adj", 0.0)
                                if adj != 0.0:
                                    old_conf = results["economic_cycle"].get("confidence", 0.5)
                                    new_conf = round(min(1.0, max(0.0, old_conf + adj)), 3)
                                    results["economic_cycle"]["confidence"] = new_conf
                                    icon = "⬆️" if adj > 0 else "⬇️"
                                    print(f"    {icon} [RESEARCH-1] D-1 confidence補正: "
                                          f"{old_conf:.2f} → {new_conf:.2f} "
                                          f"（HMM{'一致' if rd_result.get('agreement') else '不一致'}）")

                            if rd_result.get("transition_alert"):
                                logger.add(
                                    "regime_detector", "alert",
                                    f"レジーム遷移: "
                                    f"State{rd_result.get('prev_state')} → "
                                    f"State{rd_result.get('current_state')} "
                                    f"（{rd_result.get('state_label', '')}）",
                                )
                            else:
                                logger.add(
                                    "regime_detector", "success",
                                    f"State{rd_result.get('current_state')} "
                                    f"{rd_result.get('state_label','')} "
                                    f"/ conf補正{adj:+.2f}",
                                )
                    except Exception as _re:
                        print(f"  ⚠️  HMMレジーム検知エラー: {str(_re)[:80]}")
                else:
                    results["regime"] = {
                        "skipped": True, "current_state": 0,
                        "state_probs": [0.7, 0.1, 0.1, 0.1],
                        "d1_confidence_adj": 0.0
                    }

                # ── D-1のログ出力（HMM補正後） ────────────────────────
                phase = ec_result.get("phase", "B")
                # macro_snapshot_for_sector に象限情報を埋め込む
                # → sector_agents が build_result() 内で参照できる
                macro_snapshot_for_sector["_macro_phase"] = phase
                
                # 補正後の確信度をresultsから取得してログに出力
                conf = results["economic_cycle"].get("confidence", 0)
                trans = ec_result.get("transition_alert", False)
                logger.add(
                    "economic_cycle", "success",
                    f"象限{phase}: {ec_result.get('phase_label','')} "
                    f"確信度{conf*100:.0f}% "
                    f"{'⚠️フェーズ遷移' if trans else ''}",
                )

            else:
                print("  ⚠️  economic_cycle_detector.py が見つかりません")
                
        except Exception as e:
            # 欠落していたインデントを修正
            logger.add("economic_cycle", "error", str(e)[:100])
            print(f"  ⚠️  経済サイクル判定エラー: {str(e)[:80]}")
    else:
        print("  ⏩ 経済サイクル判定: スキップ（ENABLE_ECONOMIC_CYCLE=False）")

    # ── [R-5] 10年シナリオプランナー ────────────────────────────
    if getattr(cfg, "ENABLE_LONGTERM_SCENARIO", True) and _in_mode("longterm_scenario"):
        try:
            _ls = _load_local_module(
                "longterm_scenario",
                "/content/longterm_scenario.py",
                cfg.DRIVE_BASE / "longterm_scenario.py",
            )
            if _ls:
                ls_result = _ls.run_longterm_scenario(cfg, results)
                results["longterm_scenario"] = ls_result
                
                # screener 通過銘柄に長期スコアを付与
                if results.get("screener") and not ls_result.get("skipped"):
                    _ls.enrich_candidates_with_longterm(
                        results["screener"].get("candidates", []), ls_result
                    )
                    
                dominant = ls_result.get("dominant_scenario", "")
                triggers = len(ls_result.get("auto_triggers", []))
                logger.add(
                    "longterm_scenario", "success",
                    f"支配シナリオ={dominant} / トリガー{triggers}件",
                )
        except Exception as e:
            logger.add("longterm_scenario", "error", str(e)[:100])
            print(f"  ⚠️  シナリオプランナーエラー: {str(e)[:80]}")
    else:
        print("  ⏩ シナリオプランナー: スキップ")
    # ── [D-2] 長期債務サイクル指標 ───────────────────────────
    if getattr(cfg, "ENABLE_DEBT_CYCLE", True) and _in_mode("economic_cycle"):
        try:
            dc_result = _calc_debt_cycle_score(cfg, results)
            results["debt_cycle"] = dc_result
            # sector_adj が 0 以外の場合は macro_snapshot_for_sector に埋め込む
            if dc_result.get("sector_adj", 0.0) != 0.0:
                macro_snapshot_for_sector["_debt_cycle_adj"] = dc_result["sector_adj"]
                logger.add("debt_cycle", "success",
                           f"スコア:{dc_result['debt_cycle_score']}/100 "
                           f"全業種補正:{dc_result['sector_adj']:+.0f}pt")
            else:
                logger.add("debt_cycle", "success",
                           f"スコア:{dc_result['debt_cycle_score']}/100（補正なし）")
        except Exception as e:
            logger.add("debt_cycle", "error", str(e)[:100])
            print(f"  ⚠️  債務サイクル算出エラー: {str(e)[:80]}")

    # ── セクター分析（2-A 追加）─────────────────────────────────
    if cfg.ENABLE_SECTOR and cfg.IMPLEMENTED_AGENTS and _in_mode("sector"):
        print(f"\n  🏭 セクター分析を実行中（{len(cfg.IMPLEMENTED_AGENTS)} 業種）...")
        macro_snapshot_for_sector = results.get("macro_snapshot", {})
        _macro_weights = results.get("_macro_weights") or {}
    sector_results = {}   # ★ if _macro_weights の外に移動（常に初期化）
    if _macro_weights:
        # 重みを snapshot に埋め込んで sector_agents に伝達
        macro_snapshot_for_sector["_weights"] = _macro_weights
    for code in cfg.IMPLEMENTED_AGENTS:
        try:
            res = run_sector_analysis(code, macro_snapshot_for_sector)
            sector_results[code] = res
            # ── [D-2] 債務サイクル一律補正 ──────────────────
            _dc_adj = macro_snapshot_for_sector.get("_debt_cycle_adj", 0.0)
            if _dc_adj != 0.0 and isinstance(res, dict):
                _dc_cap = getattr(cfg, "SCORE_MAX_CYCLE_ADJ", 10.0)
                res["composite_score"], _dc_actual = _apply_capped_adjustment(
                    res.get("composite_score", 0), _dc_adj,
                    _dc_cap, label=f"D-2/{code}"
                )
                res["debt_cycle_adjustment"] = _dc_actual
            # ─────────────────────────────────────────────────
            # ── [D-1] 経済サイクル象限補正を適用 ──────────────
            ec_adj = results.get("economic_cycle", {}).get(
                "sector_adjustments", {}
            )
            if ec_adj and isinstance(res, dict) and code in ec_adj:
                    _delta = ec_adj[code]
                    _old_s = res.get("composite_score", 0)
                    _cap   = getattr(cfg, "SCORE_MAX_CYCLE_ADJ", 10.0)
                    res["composite_score"], _actual = _apply_capped_adjustment(
                        _old_s, _delta, _cap, label=f"D-1/{code}"
                    )
                    res["cycle_phase_adjustment"] = _actual

                    # ── [R-1] 横断産業シグナル補正 ─────────────────
                    _cs_signals = results.get("cross_sector_signals", [])
                    for _cs in _cs_signals:
                        _tw = _cs.get("signal", {}).get("tailwind", [])
                        _hw = _cs.get("signal", {}).get("headwind", [])
                        _cross_cap = getattr(cfg, "SCORE_MAX_CROSS_ADJ", 10.0)
                    _cross_total = sum(                          # 累積補正量を集計
                        x["delta"] for x in
                        res.get("cross_sector_adjustments", [])
                    )
                    if code in _tw:
                        _raw_adj = 5.0
                        # 累積上限チェック: 残余余地だけ適用
                        _headroom = _cross_cap - _cross_total
                        _adj = max(0.0, min(_raw_adj, _headroom))
                        if _adj > 0:
                            res["composite_score"] = round(
                                max(-30, min(100,
                                    res.get("composite_score", 0) + _adj)), 1)
                            res.setdefault("cross_sector_adjustments", []).append(
                                {"rule": _cs["rule_id"], "delta": +_adj})
                    elif code in _hw:
                        _raw_adj = -5.0
                        _headroom = -_cross_cap - _cross_total  # 負方向の余地
                        _adj = min(0.0, max(_raw_adj, _headroom))
                        if _adj < 0:
                            res["composite_score"] = round(
                                max(-30, min(100,
                                    res.get("composite_score", 0) + _adj)), 1)
                            res.setdefault("cross_sector_adjustments", []).append(
                                {"rule": _cs["rule_id"], "delta": _adj})
                    # ── [R-2] 先行指標シグナル補正 ─────────────────
                    _lead_signals = results.get("leading_indicator_signals", [])
                    _lead_cross_cap = getattr(cfg, "SCORE_MAX_CROSS_ADJ", 10.0)
                    for _ls in _lead_signals:
                        _ltw = _ls.get("signal", {}).get("tailwind", [])
                        _lhw = _ls.get("signal", {}).get("headwind", [])
                        _lead_total = sum(
                            x["delta"] for x in
                            res.get("cross_sector_adjustments", [])
                        )
                        if code in _ltw:
                            _lheadroom = _lead_cross_cap - _lead_total
                            _ladj = max(0.0, min(3.0, _lheadroom))  # 先行指標は控えめに+3pt上限
                            if _ladj > 0:
                                res["composite_score"] = round(
                                    max(-30, min(100,
                                        res.get("composite_score", 0) + _ladj)), 1)
                                res.setdefault("cross_sector_adjustments", []).append(
                                    {"rule": _ls["rule_id"], "delta": +_ladj,
                                    "type": "leading"})
                        elif code in _lhw:
                            _lheadroom = -_lead_cross_cap - _lead_total
                            _ladj = min(0.0, max(-3.0, _lheadroom))
                            if _ladj < 0:
                                res["composite_score"] = round(
                                    max(-30, min(100,
                                        res.get("composite_score", 0) + _ladj)), 1)
                                res.setdefault("cross_sector_adjustments", []).append(
                                    {"rule": _ls["rule_id"], "delta": _ladj,
                                    "type": "leading"})
                    # ─────────────────────────────────────────────────

                    logger.add("sector_agent", "success",
                            f"[{code}]{res.get('sector_name_ja','')} "
                            f"スコア:{res.get('composite_score','N/A')} "
                            f"リスク:{res.get('risk_level','N/A')}")
        except Exception as e:
            logger.add("sector_agent", "error", f"[{code}] {str(e)[:80]}")
        results["sectors"] = sector_results
    # ── [D-4] モメンタムファクター計算 ─────────────────────────
    if cfg.ENABLE_SECTOR and sector_results:
        try:
            import sys as _sys, importlib as _imp
            _sys.path.insert(0, str(Path(__file__).parent))
            _mf = _imp.import_module("momentum_factor")
            _sector_names = {
                c: info.get("name_ja", c)
                for c, info in cfg.JAPAN_SECTORS.items()
            }
            _mom_result = _mf.run_momentum_factor(
                sector_results = sector_results,
                data_dir       = cfg.DATA_DIR,
                sector_names   = _sector_names,
                top_n          = 5,
            )
            results["momentum"] = _mom_result
        except Exception as _e:
            print(f"  ⚠️  momentum_factor エラー（スキップ）: {_e}")
            results["momentum"] = {}
   # [S-0] factor_breakdown を score_history を使って後付け付与
        try:
            _score_hist_path = Path(cfg.DATA_DIR) / "score_history.json"
            _score_hist_all  = {}
            if _score_hist_path.exists():
                import json as _json
                _raw = _json.loads(_score_hist_path.read_text(encoding="utf-8"))
                # 形式チェック: {"YYYY-MM-DD": {"1001": {"score":float}}} の dict
                if isinstance(_raw, dict):
                    _score_hist_all = _raw
                # 旧リスト形式への後方互換（{"date":..., "scores":{...}} のリスト）
                elif isinstance(_raw, list):
                    _score_hist_all = {
                        h["date"]: h.get("scores", {})
                        for h in _raw
                        if isinstance(h, dict) and h.get("date")
                    }

            import sys as _sys, importlib as _imp
            _sys.path.insert(0, str(Path(__file__).parent))
            _se = _imp.import_module("score_explainer")
            _snapshot = results.get("macro_snapshot", {})

            for _code, _res in sector_results.items():
                if not isinstance(_res, dict):
                    continue
                # score_history の dict から当該業種の時系列を抽出
                # 形式: {date_str: {sector_code: {"score": float, "risk": str}}}
                _hist = []
                for _date_str, _day_data in sorted(_score_hist_all.items()):
                    if not isinstance(_day_data, dict):
                        continue
                    _entry = _day_data.get(_code)
                    if _entry is None:
                        continue
                    # {"score": float} 形式と float 直接の両方に対応
                    if isinstance(_entry, dict):
                        _sc = _entry.get("score")
                    else:
                        try:
                            _sc = float(_entry)
                        except (TypeError, ValueError):
                            _sc = None
                    if _sc is not None:
                        _hist.append({"date": _date_str, "composite_score": float(_sc)})

                _res["factor_breakdown"] = _se.factor_breakdown(
                    result         = _res,
                    score_history  = _hist,
                    macro_snapshot = _snapshot,
                )
            print("  ✅ factor_breakdown 付与完了")
        except Exception as _e:
            print(f"  ⚠️  factor_breakdown 付与エラー: {_e}")

        # ── [GAP-2] sector_tier 計算 ──────────────────────────
        try:
            _tier = _calc_sector_tier(sector_results, results)
            results["sector_tier"] = _tier

            # tier 別にカウントして表示
            _n_top = sum(1 for t in _tier.values() if t == "TOP")
            _n_mid = sum(1 for t in _tier.values() if t == "MID")
            _n_bot = sum(1 for t in _tier.values() if t == "BOT")
            print(f"    📊 sector_tier: TOP={_n_top} / MID={_n_mid} / BOT={_n_bot}")

            # sector_results の各業種に tier を埋め込む
            for _code, _t in _tier.items():
                if isinstance(sector_results.get(_code), dict):
                    sector_results[_code]["sector_tier"] = _t

            results["sectors"] = sector_results  # tier 付与後で上書き
        except Exception as _tier_e:
            print(f"  ⚠️  sector_tier 計算エラー: {_tier_e}")

        # ── [③] シグナル重複排除 ─────────────────────────────────
        if getattr(cfg, "ENABLE_SIGNAL_DEDUP", True):
            try:
                _sd = _load_local_module(
                    "signal_dedup",
                    "/content/signal_dedup.py",
                    cfg.DRIVE_BASE / "signal_dedup.py",
                )
                if _sd:
                    sector_results, dedup_info = _sd.run_signal_dedup(
                        sector_results, results, cfg
                    )
                    results["signal_dedup"] = dedup_info
                    if dedup_info.get("total_rejected", 0) > 0:
                        logger.add("signal_dedup", "success",
                                   f"除外{dedup_info['total_rejected']}件 / "
                                   f"上限適用{dedup_info['sectors_capped']}業種")
            except Exception as _sde:
                print(f"  ⚠️  signal_dedup エラー: {_sde}")

        # ── [GAP-1] ベイズ的スコア更新 ──────────────────────────
        if getattr(cfg, "ENABLE_BAYESIAN_UPDATE", True):
            try:
                import sys as _sys, importlib as _imp
                _sys.path.insert(0, str(Path(__file__).parent))
                _se = _imp.import_module("score_explainer")

                # score_history.json を読み込む
                _sh_path = Path(cfg.DATA_DIR) / "score_history.json"
                _sh_all  = {}
                if _sh_path.exists():
                    import json as _json
                    _sh_all = _json.loads(_sh_path.read_text(encoding="utf-8"))

                _updated, _drifts = _se.apply_bayesian_to_sector_results(
                    sector_results, _sh_all, cfg
                )
                results["sectors"] = _updated
                results["drift_events"] = _drifts

                if _drifts:
                    print(f"    ⚡ [GAP-1] ドリフト検出: {len(_drifts)}業種")
                    for d in _drifts[:3]:
                        icon = "📈" if d["direction"] == "up" else "📉"
                        print(f"       {icon} [{d['code']}] "
                              f"{d['prev']:.0f}→{d['today']:.0f}pt "
                              f"(±{d['drift']:.0f}pt / {d['severity']})")
                else:
                    print(f"    ✅ [GAP-1] ベイズ更新完了（ドリフトなし）")

                logger.add("bayesian_update", "success",
                           f"更新{len(_updated)}業種 / ドリフト{len(_drifts)}件")

                # ── [GAP-3] 乖離ログを永続記録 ──────────────
                try:
                    _pt = _load_local_module(
                        "performance_tracker",
                        "/content/performance_tracker.py",
                        cfg.DRIVE_BASE / "performance_tracker.py",
                    )
                    if _pt and hasattr(_pt, "record_score_drift"):
                        _pt.record_score_drift(cfg, _updated)
                except Exception as _ge:
                    pass  # ログ失敗はメイン処理を止めない

            except Exception as _e:
                print(f"  ⚠️  [GAP-1] ベイズ更新エラー: {str(_e)[:60]}")

            except Exception as _e:
                print(f"  ⚠️  sector_tier 計算エラー（スキップ）: {_e}")
                results["sector_tier"] = {}

        print(f"  ✅ セクター分析完了: {len(sector_results)} 業種")

    # ── [NEW] JP×US横断相関 + ローテーション検出 ─────────────────
    if getattr(cfg, "ENABLE_JP_US_BRIDGE", True) and _in_mode("jp_us_bridge"):
        try:
            _bridge = _load_local_module(
                "jp_us_sector_bridge",
                "/content/jp_us_sector_bridge.py",
                cfg.DRIVE_BASE / "jp_us_sector_bridge.py",
            )
            if _bridge:
                bridge_result = _bridge.run_jp_us_bridge(cfg, sector_results)
                results["jp_us_bridge"] = bridge_result

                # 先行シグナル補正を sector_results に適用
                _apply_capped_adjustment(
                    sector_results,
                    bridge_result.get("lead_adjustments", {}),
                    adj_key        = "us_lead_adjustment",
                    cap_config_key = "SCORE_MAX_CROSS_ADJ",
                    label          = "jp_us_bridge",
                )
                if bridge_result.get("lead_adjustments"):
                    results["sectors"] = sector_results

                n_adj = len(bridge_result.get("lead_adjustments", {}))
                n_sig = sum(1 for d in bridge_result.get("lagged_correlations", {}).values()
                            if d.get("significant"))
                logger.add("jp_us_bridge", "success",
                           f"有意相関:{n_sig}ペア 先行補正:{n_adj}業種")
        except Exception as e:
            logger.add("jp_us_bridge", "error", str(e)[:100])
            print(f"  ⚠️  JP×US横断分析エラー: {str(e)[:80]}")
    else:
        print("  ⏩ JP×US横断分析: スキップ（週次以外）")
    # ── [Phase US-5] US-JP セクターブリッジ強化版 ────────────────
    if getattr(cfg, "ENABLE_US_SECTOR_BRIDGE", True) and _in_mode("us_sector_bridge"):
        try:
            _usbe = _load_local_module(
                "us_sector_bridge_enhanced",
                "/content/us_sector_bridge_enhanced.py",
                cfg.DRIVE_BASE / "us_sector_bridge_enhanced.py",
            )
            if _usbe:
                usbe_result = _usbe.run_us_sector_bridge_enhanced(cfg, results)
                results["us_sector_bridge"] = usbe_result

                # CROSS_SECTOR シグナルに統合
                if usbe_result.get("cross_signals"):
                    existing = results.get("cross_sector_signals", [])
                    results["cross_sector_signals"] = (
                        existing + usbe_result["cross_signals"]
                    )
                    logger.add(
                        "us_sector_bridge", "success",
                        f"JP影響業種{len(usbe_result.get('jp_affected',[]))}業種 / "
                        f"beat{len(usbe_result.get('beat_tickers',[]))} / "
                        f"miss{len(usbe_result.get('miss_tickers',[]))}",
                    )
        except Exception as e:
            logger.add("us_sector_bridge", "error", str(e)[:100])
            print(f"  ⚠️  US-JP セクターブリッジエラー: {str(e)[:80]}")
    else:
        print("  ⏩ US-JP セクターブリッジ: スキップ")
    # ── [3-7] 市場幅指標（マーケットブレス / B-4） ─────────────────
    # 注: sector分析直後に実行してcomposite_scoreを補正する
    if getattr(cfg, "ENABLE_MARKET_BREADTH", True) and _in_mode("market_breadth"):
        try:
            _mb = _load_local_module(
                "market_breadth",
                "/content/market_breadth.py",
                cfg.DRIVE_BASE / "market_breadth.py",
            )
            if _mb:
                mb_result = _mb.run_market_breadth(
                    cfg            = cfg,
                    sector_results = sector_results,
                    macro_snapshot = results.get("macro_snapshot", {}),
                )
                results["market_breadth"] = mb_result

                # composite_score に市場幅補正を適用
                mb_adj = mb_result.get("macro_adjustment", {})
                _apply_capped_adjustment(
                    sector_results,
                    {k: v for k, v in mb_adj.items() if v != 0},
                    adj_key        = "breadth_adjustment",
                    cap_config_key = "SCORE_MAX_MACRO_ADJ",
                    label          = "market_breadth",
                )
                sector_results[_code]["breadth_adjustment"] = _delta
                if mb_adj:
                    results["sectors"] = sector_results

                score = mb_result.get("breadth_score_jp", 50)
                div   = mb_result.get("divergence", {})
                logger.add("market_breadth", "success",
                           f"JPスコア:{score:.0f}/100 "
                           f"補正:{len(mb_adj)}業種 "
                           f"ダイバージェンス:{div.get('detected', False)}")
            else:
                print("  ⚠️  market_breadth.py が見つかりません")
        except Exception as e:
            logger.add("market_breadth", "error", str(e)[:100])
            print(f"  ⚠️  市場幅指標エラー: {str(e)[:80]}")
    else:
        print("  ⏩ 市場幅指標: スキップ（ENABLE_MARKET_BREADTH=False / 非週次）")
    # ── [4-4] 季節アノマリー補正（D-1 / 週次） ──────────────────────
    if getattr(cfg, "ENABLE_ANOMALY_SEASON", True) and _in_mode("anomaly_season"):
        try:
            _as_mod = _load_local_module(
                "anomaly_season",
                "/content/anomaly_season.py",
                cfg.DRIVE_BASE / "anomaly_season.py",
            )
            if _as_mod:
                as_result = _as_mod.run_anomaly_season(cfg, sector_results)
                results["anomaly_season"] = as_result

                # composite_score に季節補正を適用
                _apply_capped_adjustment(
                    sector_results,
                    {k: v for k, v in mb_adj.items() if v != 0},
                    adj_key        = "season_adjustment",
                    cap_config_key = "SCORE_MAX_MACRO_ADJ",
                    label          = "ANOMALY_SEASON",
                )
                if as_result.get("adjustments"):
                    results["sectors"] = sector_results

                n_sig = sum(1 for v in as_result.get("adjustments", {}).values() if v != 0)
                logger.add("anomaly_season", "success",
                           f"{as_result.get('current_month')}月 / 有意補正:{n_sig}業種")
            else:
                print("  ⚠️  anomaly_season.py が見つかりません")
        except Exception as e:
            logger.add("anomaly_season", "error", str(e)[:100])
            print(f"  ⚠️  季節アノマリーエラー: {str(e)[:80]}")
    else:
        print("  ⏩ 季節アノマリー: スキップ（週次以外）")
    # ── [4-5] オプション市場センチメント（A-1 / 日次） ───────────────
    if getattr(cfg, "ENABLE_OPTIONS_MONITOR", True) and _in_mode("options_monitor"):
        try:
            _om = _load_local_module(
                "options_monitor",
                "/content/options_monitor.py",
                cfg.DRIVE_BASE / "options_monitor.py",
            )
            if _om:
                om_result = _om.run_options_monitor(
                    cfg,
                    sector_results=sector_results,
                )
                results["options_monitor"] = om_result

                # composite_score にオプション補正を適用
                _apply_capped_adjustment(
                    sector_results,
                    {k: v for k, v in mb_adj.items() if v != 0},
                    adj_key        = "options_adjustment",
                    cap_config_key = "SCORE_MAX_MACRO_ADJ",
                    label          = "OPTIONS_MONITOR",
                )
                if om_result.get("adjustments"):
                    results["sectors"] = sector_results

                sent = om_result.get("sentiment", {})
                logger.add("options_monitor", "success",
                           f"センチメント:{sent.get('score',50):.0f}/100 "
                           f"signal={sent.get('signal','neutral')}")
            else:
                print("  ⚠️  options_monitor.py が見つかりません")
        except Exception as e:
            logger.add("options_monitor", "error", str(e)[:100])
            print(f"  ⚠️  オプションモニターエラー: {str(e)[:80]}")
    else:
        print("  ⏩ オプションセンチメント: スキップ（ENABLE_OPTIONS_MONITOR=False）")
    # ── タイムライン分析（3-A 追加）───────────────────────────
    if getattr(cfg, "ENABLE_TIMELINE", False) and _in_mode("timeline"):
        print("\n  ⏳ タイムライン分析を実行中...")
        try:
            _tm = _load_local_module(
                "timeline_manager",
                "/content/timeline_manager.py",
                cfg.DRIVE_BASE / "timeline_manager.py",
            )
            if _tm:
                tl_result = _tm.run_timeline_analysis(cfg)
                results["timeline"] = tl_result
                gc = len(tl_result.get("long_term", {}).get("golden_cross_sectors", []))
                dc = len(tl_result.get("long_term", {}).get("dead_cross_sectors", []))
                st_len = len(tl_result.get("short_term", []))
                logger.add("timeline", "success",
                           f"長期: ゴールデンクロス {gc}業種 / デッドクロス {dc}業種 "
                           f"| 短期: {st_len}業種")
            else:
                print("  ⚠️  timeline_manager.py が見つかりません")
                logger.add("timeline", "warn", "timeline_manager.py not found")
        except Exception as e:
            logger.add("timeline", "error", str(e)[:100])
            print(f"  ⚠️  タイムライン分析エラー: {str(e)[:80]}")
    else:
        print("  ⏩ タイムライン分析: スキップ（config.ENABLE_TIMELINE=False）")

    # ── 政策金利トラッカー（3-C 追加）────────────────────────
    if getattr(cfg, "ENABLE_POLICY_TRACKER", False) and _in_mode("policy"):
        print("\n  🏦 政策金利トラッカーを実行中...")
        try:
            _pt = _load_local_module(
                "policy_tracker",
                "/content/policy_tracker.py",
                cfg.DRIVE_BASE / "policy_tracker.py",
            )
            if _pt:
                pt_result = _pt.run_policy_tracker(cfg)
                results["policy"] = pt_result
                n_new = len(pt_result.get("new_events", []))
                n_all = len(pt_result.get("all_events", []))
                logger.add("policy_tracker", "success",
                           f"新規イベント:{n_new}件 / 累積:{n_all}件")
                if n_new:
                    for ev in pt_result["new_events"]:
                        logger.add("policy_event", "warn",
                                   f"[{ev['bank']}] {ev['label']} "
                                   f"{ev['change_pt']:+.3f}%pt {ev['direction']}")
            else:
                print("  ⚠️  policy_tracker.py が見つかりません")
                logger.add("policy_tracker", "warn", "policy_tracker.py not found")
        except Exception as e:
            logger.add("policy_tracker", "error", str(e)[:100])
            print(f"  ⚠️  政策金利トラッカーエラー: {str(e)[:80]}")
    else:
        print("  ⏩ 政策金利トラッカー: スキップ（config.ENABLE_POLICY_TRACKER=False）")
    # ── [A-6] 多通貨モニタリング ──────────────────────────────────
    if getattr(cfg, "ENABLE_MULTI_CURRENCY", True) and _in_mode("multi_currency"):
        try:
            _mc = _load_local_module(
                "multi_currency_monitor",
                "/content/multi_currency_monitor.py",
                cfg.DRIVE_BASE / "multi_currency_monitor.py",
            )
            if _mc:
                mc_result = _mc.run_multi_currency(cfg)
                results["multi_currency"] = mc_result
                n_sig = len(mc_result.get("signals", []))
                n_adj = len(mc_result.get("macro_adjustment", {}))
                logger.add("multi_currency", "success",
                           f"{n_sig}シグナル / {n_adj}業種補正")
                # 多通貨補正を sector_results に反映
                mc_adj = mc_result.get("macro_adjustment", {})
                _sr    = results.get("sectors", {})
                _apply_capped_adjustment(
                    sector_results,
                    {k: v for k, v in mb_adj.items() if v != 0},
                    adj_key        = "currency_adjustment",
                    cap_config_key = "SCORE_MAX_CROSS_ADJ",
                    label          = "MULTI_CURRENCY",
                )
                _sr[_code]["currency_adjustment"] = _delta
                if mc_adj:
                    results["sectors"] = _sr
            else:
                print("  ⚠️  multi_currency_monitor.py が見つかりません")
        except Exception as e:
            logger.add("multi_currency", "error", str(e)[:100])
            print(f"  ⚠️  多通貨モニタリングエラー: {str(e)[:80]}")
    else:
        print("  ⏩ 多通貨モニタリング: スキップ（ENABLE_MULTI_CURRENCY=False）")
    # ── [2-5] FEDカレンダー取得 ────────────────────────────────────
    if getattr(cfg, "ENABLE_FED_CALENDAR", True) and _in_mode("policy"):
        print("\n  📅 FEDカレンダー取得中...")
        try:
            _fc = _load_local_module(
                "fed_calendar_fetcher",
                "/content/fed_calendar_fetcher.py",
                cfg.DRIVE_BASE / "fed_calendar_fetcher.py",
            )
            if _fc:
                fc_result = _fc.run_fed_calendar(cfg)
                results["fed_calendar"] = fc_result
                adj   = fc_result.get("macro_adjustment", 0)
                reason = fc_result.get("adjustment_rationale", "")
                fomc_days = fc_result.get("fomc", {}).get("days_until_meeting")
                boj_days  = fc_result.get("boj",  {}).get("days_until_meeting")
                logger.add(
                    "fed_calendar", "success",
                    f"FOMC:{fomc_days}日後 / 日銀:{boj_days}日後 "
                    f"| macro調整:{adj:+.1f}pt（{reason}）"
                )
            else:
                print("  ⚠️  fed_calendar_fetcher.py が見つかりません")
                logger.add("fed_calendar", "warn", "fed_calendar_fetcher.py not found")
        except Exception as e:
            logger.add("fed_calendar", "error", str(e)[:100])
            print(f"  ⚠️  FEDカレンダーエラー: {str(e)[:80]}")
    else:
        print("  ⏩ FEDカレンダー: スキップ（ENABLE_FED_CALENDAR=False）")

    # ── 米国セクター分析（新規追加）────────────────────────
    if getattr(cfg, "ENABLE_US_SECTOR", False) and _in_mode("us_sector"):
        print("\n  🇺🇸 米国セクター分析を実行中...")
        try:
            _us = _load_local_module(
                "us_sector_pipeline",
                "/content/us_sector_pipeline.py",
                cfg.DRIVE_BASE / "us_sector_pipeline.py",
            )
            if _us:
                us_result = _us.main(cfg, macro_snapshot_for_sector)
                results["us_sectors"] = us_result
                top3 = us_result.get("top3", [])
                logger.add("us_sector", "success",
                           f"Top3: {'>'.join(top3)} | "
                           f"{len(us_result.get('sectors', {}))}セクター分析完了")
            else:
                print("  ⚠️  us_sector_pipeline.py が見つかりません")
        except Exception as e:
            logger.add("us_sector", "error", str(e)[:100])
            print(f"  ⚠️  米国セクター分析エラー: {str(e)[:80]}")
    else:
        print("  ⏩ 米国セクター分析: スキップ（config.ENABLE_US_SECTOR=False）")

        # ── 債券・コモディティ分析（フェーズ2追加）────────────────
    if getattr(cfg, "ENABLE_BOND_COMMODITY", False) and _in_mode("bond_commodity"):
        print("\n  📊 債券・コモディティパイプライン実行中...")
        try:
            _bc = _load_local_module(
                "bond_commodity_pipeline",
                "/content/bond_commodity_pipeline.py",
                cfg.DRIVE_BASE / "bond_commodity_pipeline.py",
            )
            if _bc:
                bc_result = _bc.run_bond_commodity_pipeline(cfg, logger)
                results["bond_commodity"] = bc_result
                logger.add(
                    "bond_commodity", "success",
                    f"コモディティ:{bc_result.get('ok_count',0)}銘柄 "
                    f"リセッションスコア:{bc_result.get('recession_score',0)}/100 "
                    f"JP補正:{len(bc_result.get('sector_deltas',{}).get('jp',{}))}業種"
                )
            else:
                print("  ⚠️  bond_commodity_pipeline.py が見つかりません")
                logger.add("bond_commodity", "warn", "ファイルが見つかりません")
        except Exception as e:
            logger.add("bond_commodity", "error", str(e)[:100])
            print(f"  ⚠️  債券・コモディティエラー: {str(e)[:80]}")
    else:
        print("  ⏩ 債券・コモディティ: スキップ（ENABLE_BOND_COMMODITY=False）")
    # ── [RESEARCH-3] Googleトレンド分析 ─────────────────────────
    if getattr(cfg, "ENABLE_GOOGLE_TRENDS", True) and _in_mode("google_trends"):
        try:
            _gt = _load_local_module(
                "google_trends_pipeline",
                "/content/google_trends_pipeline.py",
                cfg.DRIVE_BASE / "google_trends_pipeline.py",
            )
            if _gt:
                gt_result = _gt.run_google_trends(cfg, results)
                results["google_trends"] = gt_result

                # LEADING_INDICATOR_RULES に統合
                if gt_result.get("leading_signals"):
                    existing = results.get("leading_indicator_signals", [])
                    results["leading_indicator_signals"] = (
                        existing + gt_result["leading_signals"]
                    )
                    logger.add(
                        "google_trends", "success",
                        f"追い風{gt_result.get('total_tailwinds',0)} / "
                        f"逆風{gt_result.get('total_headwinds',0)} / "
                        f"{'キャッシュ' if gt_result.get('cached') else '新規取得'}",
                    )
        except Exception as e:
            logger.add("google_trends", "error", str(e)[:100])
            print(f"  ⚠️  Googleトレンドエラー（スキップ）: {str(e)[:60]}")
    else:
        print("  ⏩ Googleトレンド: スキップ")
    # ── [policy_radar] 政府答弁・官僚PDF先回り予測 ─────────────
    if getattr(cfg, "ENABLE_POLICY_RADAR", True) and _in_mode("policy_radar"):
        try:
            _pr = _load_local_module(
                "policy_radar",
                "/content/policy_radar.py",
                cfg.DRIVE_BASE / "policy_radar.py",
            )
            if _pr:
                pr_result = _pr.run_policy_radar(cfg, results)
                results["policy_radar"] = pr_result

                # LEADING_INDICATOR_RULES に統合
                if pr_result.get("leading_signals"):
                    existing = results.get("leading_indicator_signals", [])
                    results["leading_indicator_signals"] = (
                        existing + pr_result["leading_signals"]
                    )

                # イベントカレンダー情報を results に共有
                if pr_result.get("events_30d"):
                    results["policy_events_30d"] = pr_result["events_30d"]

                logger.add(
                    "policy_radar", "success",
                    f"{pr_result.get('total_signals',0)}シグナル / "
                    f"イベント{len(pr_result.get('events_30d',[]))}件 / "
                    f"{'キャッシュ' if pr_result.get('cached') else '新規取得'}",
                )
        except Exception as e:
            logger.add("policy_radar", "error", str(e)[:100])
            print(f"  ⚠️  政策レーダーエラー（スキップ）: {str(e)[:60]}")
    else:
        print("  ⏩ 政策レーダー: スキップ")
        # ── [RESEARCH-5] FinBERT感情分析 ────────────────────────────
    if getattr(cfg, "ENABLE_FINBERT", True) and _in_mode("finbert"):
        try:
            _fb = _load_local_module(
                "finbert_analyzer",
                "/content/finbert_analyzer.py",
                cfg.DRIVE_BASE / "finbert_analyzer.py",
            )
            if _fb:
                fb_result = _fb.run_finbert_analyzer(cfg, results)
                results["sentiment"] = fb_result

                # LEADING_INDICATOR_RULES に統合
                if fb_result.get("leading_signals"):
                    existing = results.get("leading_indicator_signals", [])
                    results["leading_indicator_signals"] = (
                        existing + fb_result["leading_signals"]
                    )

                # news の by_sector 感情スコアを反映
                if results.get("news") and fb_result.get("by_sector"):
                    results["news"]["sentiment_by_sector"] = fb_result["by_sector"]

                logger.add(
                    "finbert", "success",
                    f"スコアリング{fb_result.get('articles_scored', 0)}件 "
                    f"| {len(fb_result.get('by_sector', {}))}業種 "
                    f"| {'キャッシュ' if fb_result.get('cached') else '新規'}",
                )
        except Exception as e:
            logger.add("finbert", "error", str(e)[:100])
            print(f"  ⚠️  FinBERTエラー（スキップ）: {str(e)[:60]}")
    else:
        print("  ⏩ FinBERT感情分析: スキップ")
    # ── [Phase US-2] US マクロ拡張パイプライン ──────────────────
    if getattr(cfg, "ENABLE_US_MACRO", True) and _in_mode("us_macro"):
        try:
            _um = _load_local_module(
                "us_macro_pipeline",
                "/content/us_macro_pipeline.py",
                cfg.DRIVE_BASE / "us_macro_pipeline.py",
            )
            if _um:
                um_result = _um.run_us_macro_pipeline(cfg, results)
                results["us_macro"] = um_result

                # macro_snapshot を拡充（unrate / cpi_yoy / hy_spread 等）
                snap_ext = um_result.get("macro_snapshot_ext", {})
                if snap_ext and results.get("macro_snapshot"):
                    results["macro_snapshot"].update(snap_ext)

                # LEADING_INDICATOR_RULES に統合
                if um_result.get("leading_signals"):
                    existing = results.get("leading_indicator_signals", [])
                    results["leading_indicator_signals"] = (
                        existing + um_result["leading_signals"]
                    )

                # D-2 債務サイクルスコアに HYスプレッド情報を補完
                d2_supp = um_result.get("d2_supplement", {})
                if d2_supp and results.get("debt_cycle"):
                    results["debt_cycle"]["us_macro_supplement"] = d2_supp

                score = um_result.get("composite_us_macro_score", 50)
                n_sigs = len(um_result.get("leading_signals", []))
                logger.add(
                    "us_macro", "success",
                    f"スコア:{score:.0f}/100 / シグナル:{n_sigs}件 / "
                    f"{'キャッシュ' if um_result.get('cached') else '新規取得'}",
                )
        except Exception as e:
            logger.add("us_macro", "error", str(e)[:100])
            print(f"  ⚠️  US マクロパイプラインエラー: {str(e)[:80]}")
    else:
        print("  ⏩ US マクロ拡張: スキップ")
    # ── [F-1] イールドカーブ → セクタースコア反映 ───────────────────
    _bc_result     = results.get("bond_commodity", {})
    _sector_res_f1 = results.get("sectors", {})
    if _bc_result and _sector_res_f1:
        try:
            _bc_mod = _load_local_module(
                "bond_commodity_pipeline",
                "/content/bond_commodity_pipeline.py",
                cfg.DRIVE_BASE / "bond_commodity_pipeline.py",
            )
            if _bc_mod and hasattr(_bc_mod, "apply_recession_score_to_sectors"):
                _bc_mod.apply_recession_score_to_sectors(
                    recession_score = _bc_result.get("recession_score", 0),
                    curve_analysis  = _bc_result.get("curve_analysis", {}),
                    sector_results  = _sector_res_f1,
                )
                results["sectors"] = _sector_res_f1
                logger.add("bond_commodity", "success",
                           f"[F-1] リセッション補正適用: "
                           f"recession_score={_bc_result.get('recession_score',0)}/100")
        except Exception as _e:
            print(f"  ⚠️  [F-1] リセッション補正エラー（スキップ）: {_e}")
    # ── [問題D] 動的スクリーニング候補ビルダー ───────────────────
    if getattr(cfg, "ENABLE_DYNAMIC_SCREENER", True):
        try:
            _scb = _load_local_module(
                "screener_candidate_builder",
                "/content/screener_candidate_builder.py",
                cfg.DRIVE_BASE / "screener_candidate_builder.py",
            )
            if _scb:
                scb_result = _scb.run_screener_candidate_builder(cfg, results)
                results["screener_candidates"] = scb_result
                # cfg に動的注入（stock_screener.py が参照できるよう）
                cfg.__dict__["DYNAMIC_SCREENER_TICKERS"] = scb_result["all_tickers"]
                cfg.__dict__["DYNAMIC_SCREENER_META"]    = scb_result["dynamic_meta"]
                added = scb_result["stats"].get("dynamic_added", 0)
                if added > 0:
                    logger.add("screener_candidate_builder", "success",
                               f"動的追加{added}社 / "
                               f"合計{scb_result['stats']['total']}社")
        except Exception as _scbe:
            print(f"  ⚠️  候補ビルダーエラー（スキップ）: {_scbe}")
    # ── 個別銘柄スクリーニング（Step C）─────────────────────
    if getattr(cfg, "ENABLE_SCREENER", False) and _in_mode("screener"):
        print("\n  📋 個別銘柄スクリーニングを実行中...")
        try:
            _sc = _load_local_module(
                "stock_screener",
                "/content/stock_screener.py",
                cfg.DRIVE_BASE / "stock_screener.py",
            )
            if _sc:
                sector_res = results.get("sectors", {})
                sector_tier = results.get("sector_tier", {})

                # ── [GAP-2] BOT_TIER 業種を screener 対象から除外 ──
                if sector_tier:
                    _bot_codes = {c for c, t in sector_tier.items() if t == "BOT"}
                    if _bot_codes:
                        sector_res = {
                            c: v for c, v in sector_res.items()
                            if c not in _bot_codes
                        }
                        print(f"    ⏩ [GAP-2] BOT業種 {len(_bot_codes)}件をscrener対象外: "
                              f"{sorted(_bot_codes)[:5]}")
                screen_result = _sc.run_stock_screener(
                    cfg, sector_res,
                    sector_tier_map = results.get("sector_tier", {}),
                )
                results["screener"] = screen_result

                # ── [④] セクター振り分け補完 ───────────────────────
                try:
                    _erm2 = _load_local_module(
                        "edinet_rotation_manager",
                        "/content/edinet_rotation_manager.py",
                        cfg.DRIVE_BASE / "edinet_rotation_manager.py",
                    )
                    _edb2 = _erm2.load_db(cfg) if _erm2 else {}
                    _sc.enrich_candidates_with_sector(
                        screen_result.get("candidates", []), _edb2
                    )
                    # セクター不明の候補数をログ
                    _no_sector = sum(
                        1 for c in screen_result.get("candidates", [])
                        if not c.get("sector_code")
                    )
                    if _no_sector:
                        print(f"    ⚠️  セクターコード未確定: {_no_sector}銘柄")
                    else:
                        print(f"    ✅ セクター振り分け完了")
                except Exception as _se:
                    print(f"    ⚠️  セクター振り分けエラー: {_se}")
                logger.add("stock_screener", "success",
                           f"通過:{screen_result.get('pass_count',0)}銘柄")

                # ── [C-1] VaR制約付きスクリーニング ──────────────
                if getattr(cfg, "ENABLE_VAR_CONSTRAINT", True):
                    try:
                        _st3 = _load_local_module(
                            "stress_tester",
                            "/content/stress_tester.py",
                            cfg.DRIVE_BASE / "stress_tester.py",
                        )
                        if _st3 and hasattr(_st3, "apply_var_constraint_to_screener"):
                            _var_result = _st3.apply_var_constraint_to_screener(
                                candidates   = screen_result.get("candidates", []),
                                stress_result= results.get("stress_test", {}),
                                cfg          = cfg,
                            )
                            results["var_constraint"] = _var_result
                            if _var_result.get("constrained"):
                                logger.add(
                                    "var_constraint", "success",
                                    f"VaR={_var_result['var_pct']:.2f}% → "
                                    f"Buy制限: {_var_result['max_buy_count']}銘柄",
                                )
                                print(f"    🛡️ [C-1] {_var_result['reason']}")
                    except Exception as _c1e:
                        print(f"  ⚠️  [C-1] VaR制約エラー: {_c1e}")

                # ── [C-2] 相関制約（重複業種フィルター） ──────────
                if getattr(cfg, "ENABLE_CORR_CONSTRAINT", True):
                    try:
                        _st4 = _load_local_module(
                            "stress_tester",
                            "/content/stress_tester.py",
                            cfg.DRIVE_BASE / "stress_tester.py",
                        )
                        if _st4 and hasattr(_st4, "apply_correlation_constraint"):
                            _target_cands = (
                                results.get("var_constraint", {})
                                       .get("filtered_candidates")
                                or screen_result.get("candidates", [])
                            )
                            _corr_result = _st4.apply_correlation_constraint(
                                candidates          = _target_cands,
                                correlation_heatmap = results.get("correlation_heatmap", {}),
                                cfg                 = cfg,
                            )
                            results["corr_constraint"] = _corr_result
                            if _corr_result.get("constrained"):
                                logger.add(
                                    "corr_constraint", "success",
                                    _corr_result["reason"],
                                )
                    except Exception as _c2e:
                        print(f"  ⚠️  [C-2] 相関制約エラー: {_c2e}")
                # ── [⑥] 戦略別スキーマ付与 ──────────────────────────
                try:
                    _sos = _load_local_module(
                        "strategy_output_schema",
                        "/content/strategy_output_schema.py",
                        cfg.DRIVE_BASE / "strategy_output_schema.py",
                    )
                    if _sos:
                        _target_cands = (
                            results.get("corr_constraint", {}).get("filtered_candidates")
                            or results.get("var_constraint", {}).get("filtered_candidates")
                            or screen_result.get("candidates", [])
                        )
                        _target_cands, strategy_summary = _sos.enrich_all_candidates(
                            _target_cands, cfg
                        )
                        results["strategy_summary"] = strategy_summary
                        logger.add("strategy_schema", "success",
                                   f"コア{len(strategy_summary.get('core',[]))} / "
                                   f"イベント{len(strategy_summary.get('satellite_event',[]))} / "
                                   f"小型{len(strategy_summary.get('satellite_smallcap',[]))}")
                except Exception as _se:
                    print(f"  ⚠️  戦略スキーマ付与エラー: {_se}")

                # ── screener 成功後フロー ─────────────────────────────
                passed_tickers = [
                    c["ticker"] for c in screen_result.get("candidates", [])
                    if c.get("pass_filters")
                ]

                # [4-1] テクニカルスコア
                try:
                    _ts = _load_local_module(
                        "technical_scorer",
                        "/content/technical_scorer.py",
                        cfg.DRIVE_BASE / "technical_scorer.py",
                    )
                    if _ts and getattr(cfg, "ENABLE_TECHNICAL_SCORER", True) and passed_tickers:
                        ts_result = _ts.run_technical_scorer(cfg, passed_tickers)
                        results["technical_scorer"] = ts_result
                        logger.add("technical_scorer", "success",
                                   f"{len(ts_result.get('results',{}))}銘柄スコア算出")
                except Exception as _e:
                    print(f"  ⚠️  テクニカルスコアエラー（スキップ）: {_e}")

                # [5-2] 同業種内相対評価（comps_analyzer）
                try:
                    _ca = _load_local_module(
                        "comps_analyzer",
                        "/content/comps_analyzer.py",
                        cfg.DRIVE_BASE / "comps_analyzer.py",
                    )
                    if _ca and getattr(cfg, "ENABLE_COMPS_ANALYZER", True):
                        ca_result = _ca.run_comps_analyzer(cfg, screen_result)
                        results["comps_analyzer"] = ca_result
                        comps_cache = ca_result.get("results", {})
                        if comps_cache:
                            for c in screen_result.get("candidates", []):
                                t = c.get("ticker", "")
                                cb = _ca.get_comps_bonus(t, comps_cache)
                                if cb != 0:
                                    c["comps_bonus"] = cb
                                    old_vs = c.get("value_score", 50) or 50
                                    new_vs = round(
                                        old_vs * 0.7
                                        + comps_cache[t].get("comps_score", 50) * 0.3, 1)
                                    c["value_score"]     = new_vs
                                    c["composite_score"] = round(
                                        c.get("sector_score", 50)     * 0.35
                                        + new_vs                      * 0.25
                                        + c.get("quality_score", 50)  * 0.20
                                        + c.get("technical_score", 50)* 0.20, 1)
                        n_cheap = len(ca_result.get("top_cheap", []))
                        logger.add("comps_analyzer", "success", f"割安候補: {n_cheap}銘柄")
                except Exception as _e:
                    print(f"  ⚠️  comps_analyzerエラー（スキップ）: {_e}")

                # [4-6] サポート・レジスタンス分析
                try:
                    _sr = _load_local_module(
                        "support_resistance",
                        "/content/support_resistance.py",
                        cfg.DRIVE_BASE / "support_resistance.py",
                    )
                    if _sr and getattr(cfg, "ENABLE_SUPPORT_RESISTANCE", True) and passed_tickers:
                        sr_result = _sr.run_support_resistance(cfg, passed_tickers)
                        results["support_resistance"] = sr_result
                        breakouts = len(sr_result.get("top_breakout", []))
                        logger.add("support_resistance", "success",
                                   f"{len(sr_result.get('results',{}))}銘柄 "
                                   f"ブレイクアウト候補{breakouts}件")
                except Exception as _e:
                    print(f"  ⚠️  SR分析エラー（スキップ）: {_e}")

                # [4-7] 出来高プロファイル分析
                try:
                    _vp = _load_local_module(
                        "volume_profile",
                        "/content/volume_profile.py",
                        cfg.DRIVE_BASE / "volume_profile.py",
                    )
                    if _vp and getattr(cfg, "ENABLE_VOLUME_PROFILE", True) and passed_tickers:
                        vp_result = _vp.run_volume_profile(cfg, passed_tickers)
                        results["volume_profile"] = vp_result
                        poc_cheap = len(vp_result.get("top_poc_cheap", []))
                        logger.add("volume_profile", "success",
                                   f"{len(vp_result.get('results',{}))}銘柄 "
                                   f"POC割安候補{poc_cheap}件")
                except Exception as _e:
                    print(f"  ⚠️  VP分析エラー（スキップ）: {_e}")

                # [6-3] DCF理論株価計算（週次のみ）
                if _in_mode("dcf"):
                    try:
                        _dcf = _load_local_module(
                            "dcf_valuation",
                            "/content/dcf_valuation.py",
                            cfg.DRIVE_BASE / "dcf_valuation.py",
                        )
                        if _dcf and getattr(cfg, "ENABLE_DCF_VALUATION", True) and passed_tickers:
                            dcf_result = _dcf.run_dcf_valuation(cfg, passed_tickers)
                            results["dcf_valuation"] = dcf_result
                            dcf_cache = dcf_result.get("results", {})
                            for c in screen_result.get("candidates", []):
                                t = c.get("ticker", "")
                                bonus = _dcf.get_dcf_bonus(t, dcf_cache)
                                if bonus != 0:
                                    c["dcf_bonus"] = bonus
                                    old_vs = c.get("value_score", 50) or 50
                                    c["value_score"] = round(max(0, min(100, old_vs + bonus)), 1)
                                    c["composite_score"] = round(
                                        c.get("sector_score", 50)     * 0.35
                                        + c["value_score"]            * 0.25
                                        + c.get("quality_score", 50)  * 0.20
                                        + c.get("technical_score", 50)* 0.20, 1)
                            n_under = len(dcf_result.get("top_undervalued", []))
                            logger.add("dcf_valuation", "success",
                                       f"理論株価算出:{len(dcf_cache)}銘柄 割安:{n_under}件")
                    except Exception as _e:
                        print(f"  ⚠️  DCF計算エラー（スキップ）: {_e}")

                # [NEW] screenerフィードバックループ（EDINET品質スコア統合）
                try:
                    _sfl = _load_local_module(
                        "screener_feedback_loop",
                        "/content/screener_feedback_loop.py",
                        cfg.DRIVE_BASE / "screener_feedback_loop.py",
                    )
                    if _sfl and getattr(cfg, "ENABLE_SCREENER_FEEDBACK", True):
                        sfl_result = _sfl.run_screener_feedback_loop(cfg, screen_result)
                        results["screener_feedback"] = sfl_result
                        quality_cache = sfl_result.get("quality_cache", {})
                        if quality_cache:
                            for c in screen_result.get("candidates", []):
                                t = c.get("ticker", "")
                                bonus = _sfl.get_edinet_bonus(t, quality_cache)
                                if bonus != 0:
                                    c["edinet_bonus"] = bonus
                                    c["composite_score"] = round(
                                        max(0, min(100,
                                            c.get("composite_score", 50) + bonus * 0.2
                                        )), 1)
                except Exception as _e:
                    print(f"  ⚠️  screenerフィードバックエラー（スキップ）: {_e}")

                # [A-7] 決算サプライズ分析（screener通過銘柄対象）
                try:
                    _es = _load_local_module(
                        "earnings_surprise",
                        "/content/earnings_surprise.py",
                        cfg.DRIVE_BASE / "earnings_surprise.py",
                    )
                    if _es and getattr(cfg, "ENABLE_EARNINGS_SURPRISE", True) and passed_tickers:
                        es_result = _es.run_earnings_surprise(cfg, passed_tickers)
                        results["earnings_surprise"] = es_result
                        logger.add("earnings_surprise", "success",
                                   f"ポジ{es_result.get('hit_pos',0)}社/"
                                   f"ネガ{es_result.get('hit_neg',0)}社")
                except Exception as _e:
                    print(f"  ⚠️  earnings_surprise エラー（スキップ）: {_e}")

                # [5-1] 銘柄詳細スコアカード生成
                try:
                    _sdr = _load_local_module(
                        "stock_detail_report",
                        "/content/stock_detail_report.py",
                        cfg.DRIVE_BASE / "stock_detail_report.py",
                    )
                    if _sdr and getattr(cfg, "ENABLE_STOCK_DETAIL_REPORT", True):
                        sdr_result = _sdr.run_stock_detail_report(cfg, screen_result, results)
                        results["stock_detail_report"] = sdr_result
                        logger.add("stock_detail_report", "success",
                                   f"{sdr_result.get('generated',0)}銘柄レポート生成")
                except Exception as _e:
                    print(f"  ⚠️  stock_detail_reportエラー（スキップ）: {_e}")

            else:
                print("  ⚠️  stock_screener.py が見つかりません")
        except Exception as e:
            logger.add("stock_screener", "error", str(e)[:100])
            print(f"  ⚠️  スクリーニングエラー: {str(e)[:80]}")
    else:
        if not getattr(cfg, "ENABLE_SCREENER", False):
            print("  ⏩ 個別株スクリーニング: スキップ（ENABLE_SCREENER=False）")

    # ── [M-4] TDnet 重要適時開示モニター ─────────────────────
    if getattr(cfg, "ENABLE_TDNET_MONITOR", True) and _in_mode("tdnet"):
        try:
            _tdnet = _load_local_module(
                "tdnet_monitor",
                "/content/tdnet_monitor.py",
                cfg.DRIVE_BASE / "tdnet_monitor.py",
            )
            if _tdnet:
                tdnet_result = _tdnet.run_tdnet_monitor(cfg, results)
                results["tdnet"] = tdnet_result
                if not tdnet_result.get("skipped"):
                    hits = len(tdnet_result.get("activist_hits", []))
                    logger.add(
                        "tdnet_monitor", "success",
                        f"新規{tdnet_result.get('total',0)}件 "
                        f"| 買い戻{len(tdnet_result.get('buyback',[]))} "
                        f"| 増配{len(tdnet_result.get('div_up',[]))} "
                        f"{'| ⚡触媒ヒット'+str(hits)+'件' if hits else ''}",
                    )
            else:
                print("  ⚠️  tdnet_monitor.py が見つかりません")
        except Exception as e:
            logger.add("tdnet_monitor", "error", str(e)[:100])
            print(f"  ⚠️  TDnetモニターエラー: {str(e)[:80]}")
    else:
        print("  ⏩ TDnetモニター: スキップ（ENABLE_TDNET_MONITOR=False）")
    
    # ── [P-4] 株主構成分析 ──────────────────────────────────────
    if getattr(cfg, "ENABLE_SHAREHOLDER_ANALYZER", True) and _in_mode("screener"):
        try:
            _sa = _load_local_module(
                "shareholder_analyzer",
                "/content/shareholder_analyzer.py",
                cfg.DRIVE_BASE / "shareholder_analyzer.py",
            )
            _erm = _load_local_module(
                "edinet_rotation_manager",
                "/content/edinet_rotation_manager.py",
                cfg.DRIVE_BASE / "edinet_rotation_manager.py",
            )
            if _sa and _erm:
                _edb = _erm.load_db(cfg)
                # screener通過銘柄を対象に絞る
                _sc_res = results.get("screener", {})
                _sa_tickers = [
                    c["ticker"] for c in _sc_res.get("candidates", [])
                    if c.get("pass_filters") and c.get("ticker")
                ]
                sa_result = _sa.run_shareholder_analyzer(
                    cfg, _edb, tickers=_sa_tickers or None
                )
                results["shareholder"] = sa_result
                logger.add(
                    "shareholder_analyzer", "success",
                    f"分析{sa_result['total']}銘柄 "
                    f"| 外国人急増{len(sa_result['foreign_spike'])} "
                    f"| 情報優位{len(sa_result['info_advantage'])}",
                )

                # screener candidates に shareholder_score を追記
                if _sc_res:
                    sh_results = sa_result.get("results", {})
                    for c in _sc_res.get("candidates", []):
                        tk = c.get("ticker", "")
                        if tk in sh_results:
                            sr = sh_results[tk]
                            c["shareholder_score"]   = sr.get("shareholder_score")
                            c["foreign_pct"]         = sr.get("foreign_pct")
                            c["institutional_pct"]   = sr.get("institutional_pct")
                            c["owner_pct"]           = sr.get("owner_pct")
                            c["policy_share_trend"]  = sr.get("policy_share_trend")
                            # discovery_score への補正を適用
                            disc_adj = sr.get("discovery_adj", 0.0)
                            if disc_adj:
                                c["discovery_score"] = round(
                                    min(100.0, (c.get("discovery_score") or 0) + disc_adj),
                                    1,
                                )
        except Exception as e:
            logger.add("shareholder_analyzer", "error", str(e)[:100])
            print(f"  ⚠️  株主構成分析エラー: {str(e)[:80]}")
    else:
        print("  ⏩ 株主構成分析: スキップ")
    # ── [I-3] 経営者スコア算出 ──────────────────────────────────
    if getattr(cfg, "ENABLE_CEO_EVALUATOR", True) and _in_mode("screener"):
        try:
            _ce  = _load_local_module(
                "ceo_evaluator",
                "/content/ceo_evaluator.py",
                cfg.DRIVE_BASE / "ceo_evaluator.py",
            )
            _erm = _load_local_module(
                "edinet_rotation_manager",
                "/content/edinet_rotation_manager.py",
                cfg.DRIVE_BASE / "edinet_rotation_manager.py",
            )
            if _ce and _erm:
                _edb         = _erm.load_db(cfg)
                _sc_res      = results.get("screener", {})
                _es_cache    = results.get("earnings_surprise", {}).get("results", {})
                _ce_tickers  = [
                    c["ticker"] for c in _sc_res.get("candidates", [])
                    if c.get("pass_filters")
                ]
                ce_result = _ce.run_ceo_evaluator(
                    cfg, _edb, _ce_tickers,
                    surprise_cache  = _es_cache,
                    screener_result = _sc_res,
                    results         = results,
                )
                results["ceo_scores"] = ce_result
                logger.add(
                    "ceo_evaluator", "success",
                    f"評価{ce_result['total']}社 "
                    f"| Top1: {ce_result['top5'][0] if ce_result['top5'] else 'N/A'} "
                    f"| 経営刷新期待{len(ce_result['renewal_candidates'])}社",
                )
        except Exception as e:
            logger.add("ceo_evaluator", "error", str(e)[:100])
            print(f"  ⚠️  経営者スコアエラー: {str(e)[:80]}")
    # ── [I-5] 中期経営計画達成率トラッカー ──────────────────────
    if getattr(cfg, "ENABLE_MIDTERM_TRACKER", True) and _in_mode("screener"):
        try:
            _mt = _load_local_module(
                "midterm_plan_tracker",
                "/content/midterm_plan_tracker.py",
                cfg.DRIVE_BASE / "midterm_plan_tracker.py",
            )
            _erm3 = _load_local_module(
                "edinet_rotation_manager",
                "/content/edinet_rotation_manager.py",
                cfg.DRIVE_BASE / "edinet_rotation_manager.py",
            )
            if _mt and _erm3:
                _edb3 = _erm3.load_db(cfg)
                _sc_res3 = results.get("screener", {})
                _mt_tickers = [
                    c["ticker"] for c in _sc_res3.get("candidates", [])
                    if c.get("pass_filters")
                ]
                mt_result = _mt.run_midterm_plan_tracker(
                    cfg, _edb3, _mt_tickers,
                    tdnet_result = results.get("tdnet", {}),
                )
                results["midterm_tracker"] = mt_result
                logger.add(
                    "midterm_tracker", "success",
                    f"追跡{mt_result['total']}社 "
                    f"| 超過達成{len(mt_result['overtrack_list'])} "
                    f"| 未達リスク{len(mt_result['risk_list'])}",
                )
        except Exception as e:
            logger.add("midterm_tracker", "error", str(e)[:100])
            print(f"  ⚠️  中計追跡エラー: {str(e)[:80]}")

    # ── テーマETFトラッキング（Step C'）─────────────────────
    if getattr(cfg, "ENABLE_ETF_TRACKER", False) and _in_mode("etf_tracker"):
        print("\n  🌡️  テーマETFトラッキングを実行中...")
        try:
            _etf = _load_local_module(
                "thematic_etf_tracker",
                "/content/thematic_etf_tracker.py",
                cfg.DRIVE_BASE / "thematic_etf_tracker.py",
            )
            if _etf:
                arxiv_res = results.get("arxiv")
                etf_result = _etf.run_thematic_etf_tracker(cfg, arxiv_res)
                results["etf_tracker"] = etf_result
                hot = sum(1 for t in etf_result.get("themes", [])
                          if t["signal"] == "hot")
                logger.add("etf_tracker", "success",
                           f"HOT:{hot}件 / "
                           f"TOP:{etf_result['themes'][0]['theme_name'] if etf_result['themes'] else 'N/A'}")
            else:
                print("  ⚠️  thematic_etf_tracker.py が見つかりません")
        except Exception as e:
            logger.add("etf_tracker", "error", str(e)[:100])
            print(f"  ⚠️  テーマETFエラー: {str(e)[:80]}")
    else:
        if not getattr(cfg, "ENABLE_ETF_TRACKER", False):
            print("  ⏩ テーマETF: スキップ（ENABLE_ETF_TRACKER=False）")

    # ── アイデアジェネレーター（4-A 追加）──────────────────────
    if getattr(cfg, "ENABLE_IDEA_GENERATOR", False) and _in_mode("idea"):
        print("\n  💡 アイデアジェネレーターを実行中...")
        try:
            _ig = _load_local_module(
                "idea_generator",
                "/content/idea_generator.py",
                cfg.DRIVE_BASE / "idea_generator.py",
            )
            if _ig:
                ig_result = _ig.run_idea_generator(
                    cfg,
                    sector_results = results.get("sectors", {}),
                    macro_signals  = results.get("macro_signals", []),
                )
                results["ideas"] = ig_result
                n_ideas = len(ig_result.get("ideas", []))
                logger.add("idea_generator", "success",
                           f"アイデア生成: {n_ideas}件")
                if n_ideas:
                    for idea in ig_result["ideas"][:3]:
                        logger.add("idea", "info",
                                   f"#{idea['rank']} [{idea['sector_name']}] "
                                   f"スコア:{idea['idea_score']}")
            else:
                print("  ⚠️  idea_generator.py が見つかりません")
                logger.add("idea_generator", "warn", "idea_generator.py not found")
        except Exception as e:
            logger.add("idea_generator", "error", str(e)[:100])
            print(f"  ⚠️  アイデアジェネレーターエラー: {str(e)[:80]}")
    else:
        print("  ⏩ アイデアジェネレーター: スキップ（config.ENABLE_IDEA_GENERATOR=False）")

    # ── アイデアエバリュエーター（4-B 追加）────────────────────
    if getattr(cfg, "ENABLE_IDEA_EVALUATOR", False) and _in_mode("evaluation"):
        print("\n  📊 アイデアエバリュエーターを実行中...")
        try:
            _ie = _load_local_module(
                "idea_evaluator",
                "/content/idea_evaluator.py",
                cfg.DRIVE_BASE / "idea_evaluator.py",
            )
            if _ie:
                latest_ideas = results.get("ideas", {}).get("ideas", [])
                ie_result = _ie.run_idea_evaluator(cfg, latest_ideas)
                results["evaluations"] = ie_result
                logger.add("idea_evaluator", "success",
                           f"新規登録:{ie_result.get('added',0)}件 / "
                           f"総計:{ie_result.get('total',0)}件")
            else:
                print("  ⚠️  idea_evaluator.py が見つかりません")
                logger.add("idea_evaluator", "warn", "idea_evaluator.py not found")
        except Exception as e:
            logger.add("idea_evaluator", "error", str(e)[:100])
            print(f"  ⚠️  アイデアエバリュエーターエラー: {str(e)[:80]}")
    else:
        print("  ⏩ アイデアエバリュエーター: スキップ（config.ENABLE_IDEA_EVALUATOR=False）")

    # ── 異常検知スキャン（フェーズ2追加）──────────────────────
    if getattr(cfg, "ENABLE_ANOMALY_DETECTOR", False) and _in_mode("anomaly"):
        print("\n  🔍 異常検知スキャン実行中...")
        try:
            _ad = _load_local_module(
                "anomaly_detector",
                "/content/anomaly_detector.py",
                cfg.DRIVE_BASE / "anomaly_detector.py",
            )
            if _ad:
                ad_result = _ad.run_anomaly_detector(cfg, results, logger)
                results["anomaly"] = ad_result
                logger.add(
                    "anomaly_detector", "success",
                    f"スキャン:{ad_result.get('scan_count',0)}銘柄 "
                    f"異常:{ad_result.get('anomaly_count',0)}件 "
                    f"（複合:{len(ad_result.get('combined_anomalies',[]))}件）"
                )
            else:
                print("  ⚠️  anomaly_detector.py が見つかりません")
                logger.add("anomaly_detector", "warn", "ファイルが見つかりません")
        except Exception as e:
            logger.add("anomaly_detector", "error", str(e)[:100])
            print(f"  ⚠️  異常検知エラー: {str(e)[:80]}")
    else:
        print("  ⏩ 異常検知: スキップ（ENABLE_ANOMALY_DETECTOR=False）")
        
    # ── アラート通知（5-B 追加）────────────────────────────────
    if getattr(cfg, "ENABLE_ALERT", False):
        try:
            _an = _load_local_module(
                "alert_notifier",
                "/content/alert_notifier.py",
                cfg.DRIVE_BASE / "alert_notifier.py",
            )
            if _an and isinstance(results, dict):
                an_result = _an.run_alert_notifier(cfg, results)
                results["alerts"] = an_result
                if not an_result.get("skipped"):
                    logger.add("alert_notifier", "success",
                               f"送信:{an_result.get('alerts_sent',0)}件 "
                               f"チャネル:{','.join(an_result.get('channels',[]))}")
                for err in an_result.get("errors", []):
                    logger.add("alert_notifier", "warn", err[:80])
            elif _an:
                print(f"  ⚠️ アラート通知をスキップ: results が辞書型ではありません (型: {type(results).__name__})")
            else:
                print("  ⚠️  alert_notifier.py が見つかりません")
                logger.add("alert_notifier", "warn", "alert_notifier.py not found")
        except Exception as e:
            logger.add("alert_notifier", "error", str(e)[:100])
            print(f"  ⚠️  アラート通知エラー: {str(e)[:80]}")
    else:
        print("  ⏩ アラート通知: スキップ（config.ENABLE_ALERT=False）")

# ── IMF グローバルマクロ（デイリー）───────────────────────
    if getattr(cfg, "ENABLE_IMF", False) and _in_mode("imf"):
        print("\n  🌍 IMFグローバルマクロパイプラインを実行中...")
        try:
            _imf = _load_local_module(
                "imf_pipeline",
                "/content/imf_pipeline.py",
                cfg.DRIVE_BASE / "imf_pipeline.py",
            )
            if _imf:
                imf_result = _imf.run_imf_pipeline(cfg)
                results["imf"] = imf_result
                logger.add("imf_pipeline", "success", imf_result.get("summary", ""))
            else:
                print("  ⚠️  imf_pipeline.py が見つかりません")
        except Exception as e:
            logger.add("imf_pipeline", "error", str(e)[:100])
            print(f"  ⚠️  IMFエラー: {str(e)[:80]}")
    else:
        if not getattr(cfg, "ENABLE_IMF", False):
            print("  ⏩ IMFパイプライン: スキップ（ENABLE_IMF=False）")

    # ── Step E: SEC/有報要約（無効化中・Claude API有料のため）────
    if getattr(cfg, "ENABLE_SEC_SUMMARIZER", False) and _in_mode("sec_summary"):
        try:
            _ss = _load_local_module(
                "sec_summarizer",
                "/content/sec_summarizer.py",
                cfg.DRIVE_BASE / "sec_summarizer.py",
            )
            if _ss:
                screener_res = results.get("screener", {"candidates": []})
                ss_result = _ss.run_sec_summarizer(cfg, screener_res)
                results["sec_summary"] = ss_result
                logger.add("sec_summarizer", "success",
                           f"成功:{ss_result.get('success_count',0)}件")
            else:
                print("  ⚠️  sec_summarizer.py が見つかりません")
        except Exception as e:
            logger.add("sec_summarizer", "error", str(e)[:100])
            print(f"  ⚠️  要約エラー: {str(e)[:80]}")
    else:
        print("  ⏩ SEC/有報要約: スキップ（ENABLE_SEC_SUMMARIZER=False）")

    # ── 米国株スクリーニング（us_stock_screener）──────────────
    if getattr(cfg, "ENABLE_US_SCREENER", False) and _in_mode("us_screener"):
        print("\n  🇺🇸 米国株スクリーニング...")
        try:
            _us = _load_local_module(
                "us_stock_screener",
                "/content/us_stock_screener.py",
                cfg.DRIVE_BASE / "us_stock_screener.py",
            )
            if _us:
                us_screen_result = _us.run_us_stock_screener(cfg, results)
                results["us_screener"] = us_screen_result
                logger.add("us_screener", "success",
                           f"通過:{us_screen_result.get('pass_count',0)}社 "
                           f"スクリーニング:{us_screen_result.get('screened_count',0):,}社 "
                           f"所要:{us_screen_result.get('elapsed_min',0):.1f}分")
            else:
                print("  ⚠️  us_stock_screener.py が見つかりません")
        except Exception as e:
            logger.add("us_screener", "error", str(e)[:100])
            print(f"  ⚠️  米国株スクリーニングエラー: {str(e)[:80]}")
    else:
        if not getattr(cfg, "ENABLE_US_SCREENER", False):
            print("  ⏩ 米国株スクリーニング: スキップ（ENABLE_US_SCREENER=False）")

    # ── EdinetDB ローテーション取得 ──────────────────────────
    if getattr(cfg, "ENABLE_EDINET_ROTATION", False) and _in_mode("edinet_rotation"):
        print("\n  🔄 EdinetDB ローテーション...")
        try:
            _erm = _load_local_module(
                "edinet_rotation_manager",
                "/content/edinet_rotation_manager.py",
                cfg.DRIVE_BASE / "edinet_rotation_manager.py",
            )
            if _erm:
                # ── [問題C] sector_tier を results に含めて渡す ─────
                # sector_tier が未確定の場合は sector composite_score から簡易計算
                if "sector_tier" not in results and results.get("sectors"):
                    _top_th = getattr(cfg, "SECTOR_TIER_TOP_THRESHOLD", 65.0)
                    _bot_th = getattr(cfg, "SECTOR_TIER_MID_THRESHOLD",  45.0)
                    results["sector_tier"] = {
                        code: (
                            "TOP" if sec.get("composite_score", 0) >= _top_th else
                            "BOT" if sec.get("composite_score", 0) <  _bot_th else
                            "MID"
                        )
                        for code, sec in results["sectors"].items()
                        if isinstance(sec, dict)
                    }
                erm_result = _erm.run_edinet_rotation(cfg, results)
                results["edinet_rotation"] = erm_result
                logger.add("edinet_rotation", "success",
                           f"取得:{erm_result.get('ok_count',0)}社 "
                           f"カバレッジ:{erm_result.get('db_coverage',0):.1f}%")
        except Exception as e:
            logger.add("edinet_rotation", "error", str(e)[:100])
    # ── [Phase US-1] US財務DBローテーター ────────────────────────
    if getattr(cfg, "ENABLE_US_FUNDAMENTALS", True) and _in_mode("us_fundamentals"):
        try:
            _ufr = _load_local_module(
                "us_fundamental_rotator",
                "/content/us_fundamental_rotator.py",
                cfg.DRIVE_BASE / "us_fundamental_rotator.py",
            )
            if _ufr:
                ufr_result = _ufr.run_us_fundamental_rotator(cfg, results)
                results["us_fundamentals"] = ufr_result
                if not ufr_result.get("skipped"):
                    logger.add(
                        "us_fundamentals", "success",
                        f"取得:{ufr_result.get('ok_count',0)}社 "
                        f"カバレッジ:{ufr_result.get('db_coverage',0):.1f}% "
                        f"8K再取得:{len(ufr_result.get('refresh_8k',[]))}社",
                    )
        except Exception as e:
            logger.add("us_fundamentals", "error", str(e)[:100])
            print(f"  ⚠️  US財務DBエラー: {str(e)[:80]}")
    else:
        print("  ⏩ US財務DBローテーター: スキップ")
    # ── [Phase US-3] US 8-K リアルタイム監視 ─────────────────────
    if getattr(cfg, "ENABLE_US_8K_MONITOR", True) and _in_mode("us_8k"):
        try:
            _8k = _load_local_module(
                "us_8k_monitor",
                "/content/us_8k_monitor.py",
                cfg.DRIVE_BASE / "us_8k_monitor.py",
            )
            if _8k:
                k8_result = _8k.run_us_8k_monitor(cfg, results)
                results["us_8k"] = k8_result
                if not k8_result.get("skipped"):
                    logger.add(
                        "us_8k", "success",
                        f"決算{len(k8_result.get('earnings_tickers',[]))} / "
                        f"CEO交代{len(k8_result.get('ceo_change_tickers',[]))} / "
                        f"screener照合{len(k8_result.get('screener_hits',[]))}",
                    )
        except Exception as e:
            logger.add("us_8k", "error", str(e)[:100])
            print(f"  ⚠️  US 8-K モニターエラー: {str(e)[:80]}")
    # ── Step E代替: 事業概要フェッチャー（BizSummary）──────────
    if getattr(cfg, "ENABLE_BIZ_SUMMARY", False) and _in_mode("biz_summary"):
        print("\n  📋 事業概要フェッチャー (Step E代替)...")
        try:
            _bs = _load_local_module(
                "business_summary_fetcher",
                "/content/business_summary_fetcher.py",
                cfg.DRIVE_BASE / "business_summary_fetcher.py",
            )
            if _bs:
                bs_result = _bs.run_business_summary_fetcher(cfg, results)
                results["biz_summaries"] = bs_result
                logger.add("biz_summary", "success",
                           f"{len(bs_result.get('ticker_summaries', {}))}社取得 "
                           f"DeepL:{bs_result.get('total_chars_used', 0):,}字")
            else:
                print("  ⚠️  business_summary_fetcher.py が見つかりません")
        except Exception as e:
            logger.add("biz_summary", "error", str(e)[:100])
            print(f"  ⚠️  事業概要フェッチャーエラー: {str(e)[:80]}")
    else:
        if not getattr(cfg, "ENABLE_BIZ_SUMMARY", False):
            print("  ⏩ 事業概要取得: スキップ（ENABLE_BIZ_SUMMARY=False）")
    # ── [フェーズ3] シナリオ分析（月曜日のみ・週次レポートに統合） ─────
    if getattr(cfg, "ENABLE_SCENARIO_ANALYSIS", True) and _in_mode("weekly_report"):
        try:
            _sa = _load_local_module(
                "scenario_analyzer",
                "/content/scenario_analyzer.py",
                cfg.DRIVE_BASE / "scenario_analyzer.py",
            )
            if _sa:
                _macro_snap = results.get("macro_snapshot", {})
                _sector_res = results.get("sectors", {})
                if _macro_snap and _sector_res:
                    sa_result = _sa.run_scenario_analysis(
                        cfg         = cfg,
                        sector_results   = _sector_res,
                        macro_snapshot   = _macro_snap,
                        scenario_keys    = ["bear_case", "base_case", "bull_case",
                                            "weak_yen", "china_shock"],
                        fetch_portfolio_prices = False,  # 速度優先: Trueにすると+20秒
                    )
                    results["scenario_analysis"] = sa_result
                    logger.add("scenario_analysis", "success",
                               f"{len(sa_result.get('scenarios', {}))}シナリオ完了")
                else:
                    print("  ⏩ シナリオ分析: macro_snapshot または sectors がなし")
            else:
                print("  ⚠️  scenario_analyzer.py が見つかりません")
        except Exception as e:
            logger.add("scenario_analysis", "error", str(e)[:100])
            print(f"  ⚠️  シナリオ分析エラー: {str(e)[:80]}")
     # ── [3-5] 信用残高データ取得（A-2 / 週次） ──────────────────────
    if getattr(cfg, "ENABLE_CREDIT_DATA", True) and _in_mode("credit_data"):
        try:
            _cd = _load_local_module(
                "credit_data",
                "/content/credit_data.py",
                cfg.DRIVE_BASE / "credit_data.py",
            )
            if _cd:
                cd_result = _cd.run_credit_data(cfg)
                results["credit_data"] = cd_result
                sq = len(cd_result.get("flag_short_squeeze", []))
                oh = len(cd_result.get("flag_overheated", []))
                logger.add("credit_data", "success",
                           f"踏み上げ候補:{sq}件 / 過熱:{oh}件 "
                           f"({cd_result.get('total',0)}銘柄)")
            else:
                print("  ⚠️  credit_data.py が見つかりません")
        except Exception as e:
            logger.add("credit_data", "error", str(e)[:100])
            print(f"  ⚠️  信用残高取得エラー: {str(e)[:80]}")
    else:
        print("  ⏩ 信用残高: スキップ（ENABLE_CREDIT_DATA=False / 週次）")
    # ── [3-6] ストレステスト（C-5 / 週次・月次Gmail） ──────────────
    if getattr(cfg, "ENABLE_STRESS_TEST", True) and _in_mode("weekly_report"):
        try:
            _st = _load_local_module(
                "stress_tester",
                "/content/stress_tester.py",
                cfg.DRIVE_BASE / "stress_tester.py",
            )
            if _st:
                st_result = _st.run_stress_test(
                    cfg            = cfg,
                    macro_snapshot = results.get("macro_snapshot", {}),
                )
                results["stress_test"] = st_result
                # ── [A-2] コスト控除後リターン計算 ──────────────
                if getattr(cfg, "ENABLE_NET_RETURN_CALC", True):
                    try:
                        _st2 = _load_local_module(
                            "stress_tester",
                            "/content/stress_tester.py",
                            cfg.DRIVE_BASE / "stress_tester.py",
                        )
                        if _st2 and hasattr(_st2, "enrich_with_net_returns"):
                            results["stress_test"] = _st2.enrich_with_net_returns(
                                results.get("stress_test", {}), cfg
                            )
                    except Exception as _a2e:
                        pass
                var_d = st_result.get("var_95_1day", {})
                logger.add("stress_test", "success",
                           f"VaR(95%): {var_d.get('var_pct',0):.2f}% "
                           f"月次Mail: {st_result.get('mail_sent', False)}")
            else:
                print("  ⚠️  stress_tester.py が見つかりません")
        except Exception as e:
            logger.add("stress_test", "error", str(e)[:100])
            print(f"  ⚠️  ストレステストエラー: {str(e)[:80]}")
    else:
        print("  ⏩ ストレステスト: スキップ（週次以外）")
    # ── [4-2] インサイダー取引モニター（A-3 / 週次） ─────────────────
    if getattr(cfg, "ENABLE_INSIDER_MONITOR", True) and _in_mode("insider_monitor"):
        try:
            _im = _load_local_module(
                "insider_monitor",
                "/content/insider_monitor.py",
                cfg.DRIVE_BASE / "insider_monitor.py",
            )
            if _im:
                # screener通過銘柄を優先対象に
                screener_tickers = [
                    c["ticker"]
                    for c in results.get("screener", {}).get("candidates", [])
                    if c.get("pass_filters")
                ]
                im_result = _im.run_insider_monitor(cfg, screener_tickers or None)
                results["insider_monitor"] = im_result
                logger.add("insider_monitor", "success",
                           f"US:{im_result.get('total_us',0)}件 "
                           f"JP:{im_result.get('total_jp',0)}件 "
                           f"ボーナス適用:{len(im_result.get('bonuses',{}))}銘柄")
        except Exception as e:
            logger.add("insider_monitor", "error", str(e)[:100])
            print(f"  ⚠️  インサイダーモニターエラー: {str(e)[:80]}")
    else:
        print("  ⏩ インサイダーモニター: スキップ（週次以外）")
    # ── [4-3] 配当権利確定カレンダー（A-4 / 週次） ───────────────────
    if getattr(cfg, "ENABLE_DIVIDEND_CALENDAR", True) and _in_mode("dividend_calendar"):
        try:
            _dc = _load_local_module(
                "dividend_calendar",
                "/content/dividend_calendar.py",
                cfg.DRIVE_BASE / "dividend_calendar.py",
            )
            if _dc:
                dc_result = _dc.run_dividend_calendar(cfg)
                results["dividend_calendar"] = dc_result
                upcoming = len(dc_result.get("upcoming_30d", []))
                urgent   = len(dc_result.get("urgent_7d", []))
                logger.add("dividend_calendar", "success",
                           f"30日以内:{upcoming}銘柄 / 7日以内緊急:{urgent}銘柄")
        except Exception as e:
            logger.add("dividend_calendar", "error", str(e)[:100])
            print(f"  ⚠️  配当カレンダーエラー: {str(e)[:80]}")
    else:
        print("  ⏩ 配当カレンダー: スキップ（週次以外）")

    # ── [4-8] ルール重み最適化 & ML補正（月末のみ / WEEKLY_MODULES） ──────────
    if _in_mode("rule_optimizer"):
        # 1. ルール重み最適化
        if getattr(cfg, "ENABLE_RULE_OPTIMIZER", True):
            try:
                _ro = _load_local_module(
                    "rule_optimizer",
                    "/content/rule_optimizer.py",
                    cfg.DRIVE_BASE / "rule_optimizer.py",
                )
                if _ro:
                    ro_result = _ro.run_rule_optimizer(cfg)
                    results["rule_optimizer"] = ro_result
                # 更新有無に関わらず有効な重みを常に results に注入する
                # 月末: 今回更新した新重みを使用
                # 月末以外: rule_weights.json に保存済みの重みを読み込む
                    if ro_result.get("updated"):
                        effective_weights = ro_result.get("weights", {})
                        n_changed = sum(1 for d in effective_weights.values() if abs(d.get("delta", 0)) > 0.01)
                        logger.add("rule_optimizer", "success", f"重み更新: {n_changed}ファクター変化")
                    else:
                        effective_weights = _ro.load_weights(cfg)
                        logger.add("rule_optimizer", "skipped", ro_result.get("reason", "月末以外"))

                    if effective_weights:
                        results["_macro_weights"] = effective_weights
                        print(f"    📐 rule_weights 注入: {len(effective_weights)}ファクター")
            except Exception as e:
                logger.add("rule_optimizer", "error", str(e)[:100])
                print(f"  ⚠️  ルールオプティマイザーエラー: {str(e)[:80]}")
                # ── [⑤] 月末のみ: 監査ログ生成 ─────────────────────
                from datetime import date as _date
                _is_month_end = (_date.today() == (
                    _date.today().replace(day=1)
                    + __import__("datetime").timedelta(days=32)
                ).replace(day=1) - __import__("datetime").timedelta(days=1))
                if _is_month_end:
                    try:
                        _al = _load_local_module(
                            "learning_audit_log",
                            "/content/learning_audit_log.py",
                            cfg.DRIVE_BASE / "learning_audit_log.py",
                        )
                        if _al:
                            audit = _al.generate_audit_log(cfg, ro_result, results)
                            results["learning_audit"] = audit
                            if audit.get("manual_review_needed"):
                                logger.add("learning_audit", "alert",
                                            audit.get("review_reason", "")[:80])
                    except Exception as _ae:
                        print(f"  ⚠️  監査ログ生成エラー: {_ae}")

        # 2. [GAP-8] 月末に IC 計算を実行 (最適化の成否に関わらず試行)
        try:
            _aa = _load_local_module(
                "alpha_attributer",
                "/content/alpha_attributer.py",
                cfg.DRIVE_BASE / "alpha_attributer.py",
            )
            if _aa:
                aa_result = _aa.run_alpha_attributer(cfg, results)
                results["alpha_attribution"] = aa_result
                if aa_result.get("calculated"):
                    top = aa_result.get("attribution", {}).get("top_signals", [])
                    logger.add("alpha_attributer", "success", f"IC計算完了: TOP={top[:3]}")
        except Exception as _ae:
            print(f"    ⚠️  IC計算エラー: {_ae}")

        # 3. [RESEARCH-4] MLスコア補正レイヤー (指示通りブロックの末尾に配置)
        if getattr(cfg, "ENABLE_ML_CORRECTION", False):
            try:
                _ml = _load_local_module(
                    "ml_score_corrector",
                    "/content/ml_score_corrector.py",
                    cfg.DRIVE_BASE / "ml_score_corrector.py",
                )
                if _ml:
                    ml_result = _ml.run_ml_score_corrector(cfg, results)
                    results["ml_correction"] = ml_result
                    # screener candidates に補正を適用
                    corrections = ml_result.get("corrections", {})
                    if corrections and not ml_result.get("skipped"):
                        applied = 0
                        for c in results.get("screener", {}).get("candidates", []):
                            tk = c.get("ticker", "")
                            if tk in corrections:
                                adj = corrections[tk]
                                old_score = c.get("composite_score") or 50.0
                                new_score = round(min(100.0, max(0.0, old_score + adj)), 1)
                                c["composite_score"] = new_score
                                c["ml_correction"] = adj
                                applied += 1

                        ic = ml_result.get("model_ic")
                        logger.add("ml_correction", "success", 
                                   f"補正適用{applied}銘柄" + (f" IC={ic:.4f}" if ic else ""))
            except Exception as e:
                logger.add("ml_correction", "error", str(e)[:100])
                print(f"  ⚠️  ML補正エラー（スキップ）: {str(e)[:60]}")
    else:
        print("  ⏩ ルールオプティマイザー/ML補正: スキップ")
    # ── [5-3] バックテスト（C-2 / 週次） ────────────────────────
    if getattr(cfg, "ENABLE_BACKTESTER", True) and _in_mode("backtester"):
        try:
            _bt = _load_local_module(
                "backtester",
                "/content/backtester.py",
                cfg.DRIVE_BASE / "backtester.py",
            )
            if _bt:
                bt_result = _bt.run_backtester(cfg)
                results["backtest"] = bt_result
                if not bt_result.get("skipped"):
                    ov = bt_result.get("overall", {})
                    logger.add("backtester", "success",
                               f"評価:{bt_result.get('evaluated_count',0)}銘柄 "
                               f"α={ov.get('avg_alpha',0):+.2f}% "
                               f"勝率={ov.get('win_rate',0):.1%}")
                else:
                    logger.add("backtester", "skipped",
                               bt_result.get("reason", "データ不足"))
        except Exception as e:
            logger.add("backtester", "error", str(e)[:100])
            print(f"  ⚠️  バックテストエラー: {str(e)[:80]}")
    else:
        print("  ⏩ バックテスト: スキップ（週次以外）")
            # ── [5-5] パフォーマンス自己検証 ────────────────────────────
    if getattr(cfg, "ENABLE_SELF_EVALUATOR", True) and _in_mode("self_evaluator"):
        try:
            _se = _load_local_module(
                "self_evaluator",
                "/content/self_evaluator.py",
                cfg.DRIVE_BASE / "self_evaluator.py",
            )
            if _se:
                se_result = _se.run_self_evaluator(cfg)
                results["self_evaluator"] = se_result
                if not se_result.get("skipped"):
                    s = se_result.get("summary", {})
                    logger.add("self_evaluator", "success",
                               f"通算勝率:{s.get('lifetime_win_rate',0):.1%} "
                               f"トレンド:{s.get('trend','—')} "
                               f"悪因子:{s.get('worst_factor','—')}")
                else:
                    logger.add("self_evaluator", "skipped",
                               se_result.get("reason", "データ不足"))
        except Exception as e:
            logger.add("self_evaluator", "error", str(e)[:100])
            print(f"  ⚠️  自己検証エラー: {str(e)[:80]}")
    else:
        print("  ⏩ 自己検証: スキップ（週次以外）")

    # ── [c] screenerセクターヒートマップ ─────────────────────────
    if getattr(cfg, "ENABLE_SCREENER_HEATMAP", True) and _in_mode("screener_heatmap"):
        try:
            _ssh = _load_local_module(
                "screener_sector_heatmap",
                "/content/screener_sector_heatmap.py",
                cfg.DRIVE_BASE / "screener_sector_heatmap.py",
            )
            if _ssh:
                ssh_result = _ssh.run_screener_sector_heatmap(cfg)
                results["screener_heatmap"] = ssh_result
                if not ssh_result.get("skipped"):
                    rot_in  = ssh_result.get("rotation_in", [])
                    rot_out = ssh_result.get("rotation_out", [])
                    logger.add("screener_heatmap", "success",
                               f"流入:{rot_in[:3]} 流出:{rot_out[:3]}")
        except Exception as e:
            logger.add("screener_heatmap", "error", str(e)[:100])
            print(f"  ⚠️  ヒートマップエラー: {str(e)[:80]}")
    else:
        print("  ⏩ セクターヒートマップ: スキップ（週次以外）")
    # ── [6-1] ポートフォリオ最適化（C-1 / 週次） ────────────────
    if getattr(cfg, "ENABLE_PORTFOLIO_OPTIMIZER", True) and _in_mode("portfolio_optimizer"):
        try:
            _po = _load_local_module(
                "portfolio_optimizer",
                "/content/portfolio_optimizer.py",
                cfg.DRIVE_BASE / "portfolio_optimizer.py",
            )
            if _po:
                po_result = _po.run_portfolio_optimizer(cfg)
                results["portfolio_optimizer"] = po_result
                if not po_result.get("skipped"):
                    total   = po_result.get("current", {}).get("total_value_jpy", 0)
                    n_rebal = sum(1 for r in po_result.get("rebalance", [])
                                  if r.get("action") != "HOLD")
                    logger.add("portfolio_optimizer", "success",
                               f"総時価:{total:,.0f}円 "
                               f"リバランシング推奨:{n_rebal}銘柄")
                else:
                    logger.add("portfolio_optimizer", "skipped",
                               po_result.get("reason", "ポートフォリオデータなし"))
        except Exception as e:
            logger.add("portfolio_optimizer", "error", str(e)[:100])
            print(f"  ⚠️  ポートフォリオ最適化エラー: {str(e)[:80]}")
    else:
        print("  ⏩ ポートフォリオ最適化: スキップ（週次以外）")
    # ── Step F: 週次投資判断レポート ─────────────────────────────
    if getattr(cfg, "ENABLE_WEEKLY_REPORT", True) and _in_mode("weekly_report"):
        print("\n  📊 週次投資判断レポート生成 (Step F)...")
        try:
            _wr = _load_local_module(
                "weekly_report_generator",
                "/content/weekly_report_generator.py",
                cfg.DRIVE_BASE / "weekly_report_generator.py",
            )
            if _wr:
                wr_result = _wr.run_weekly_report(cfg, results)
                results["weekly_report"] = wr_result
                if not wr_result.get("skipped"):
                    logger.add("weekly_report", "success",
                               f"レポート生成: {wr_result.get('report_path','')}")
            else:
                print("  ⚠️  weekly_report_generator.py が見つかりません")
        except Exception as e:
            logger.add("weekly_report", "error", str(e)[:100])
            print(f"  ⚠️  週次レポートエラー: {str(e)[:80]}")
    else:
        if not getattr(cfg, "ENABLE_WEEKLY_REPORT", True):
            print("  ⏩ 週次レポート: スキップ（ENABLE_WEEKLY_REPORT=False）")
    
        # ── [3-2] 相関ヒートマップ計算（C-4） ──────────────────────────
    if getattr(cfg, "ENABLE_CORRELATION_HEATMAP", True) and _in_mode("excel_export"):
        try:
            _ch = _load_local_module(
                "correlation_heatmap",
                "/content/correlation_heatmap.py",
                cfg.DRIVE_BASE / "correlation_heatmap.py",
            )
            if _ch:
                ch_result = _ch.run_correlation_heatmap(cfg)
                results["correlation_heatmap"] = ch_result
                conc = ch_result.get("concentration", {})
                logger.add("correlation_heatmap", "success",
                           f"{len(ch_result.get('tickers', []))}銘柄 "
                           f"集中リスク:{conc.get('risk_level','N/A')} "
                           f"{conc.get('summary','')[:40]}")
        except Exception as e:
            logger.add("correlation_heatmap", "error", str(e)[:100])
            print(f"  ⚠️  相関ヒートマップエラー: {str(e)[:80]}")
    else:
        print("  ⏩ 相関ヒートマップ: スキップ（ENABLE_CORRELATION_HEATMAP=False）")
    # ── [3-3] ポジションサイジング（C-3）─────────────────────────
    if getattr(cfg, "ENABLE_POSITION_SIZER", True) and _in_mode("weekly_report"):
        try:
            _ps = _load_local_module(
                "position_sizer",
                "/content/position_sizer.py",
                cfg.DRIVE_BASE / "position_sizer.py",
            )
            if _ps:
                ps_result = _ps.run_position_sizer(
                    cfg            = cfg,
                    total_assets_jpy = getattr(cfg, "TOTAL_ASSETS_JPY", 10_000_000),
                    risk_pct       = getattr(cfg, "POSITION_RISK_PCT", 0.01),
                    atr_multiplier = getattr(cfg, "POSITION_ATR_MULT", 2.0),
                    sector_results = results.get("sectors", {}),
                    screener_result  = results.get("screener", {}),   # ★ I-4追加
                    results          = results,                        # ★ I-4追加
                )
                results["position_sizer"] = ps_result
                valid = [p for p in ps_result.get("positions", []) if not p.get("error")]
                logger.add("position_sizer", "success",
                           f"{len(valid)}銘柄 サイジング完了")
            else:
                print("  ⚠️  position_sizer.py が見つかりません")
        except Exception as e:
            logger.add("position_sizer", "error", str(e)[:100])
            print(f"  ⚠️  ポジションサイジングエラー: {str(e)[:80]}")
    else:
        print("  ⏩ ポジションサイジング: スキップ（週次/無効）")
    # ── Excel ダッシュボード CSV エクスポート（Step D）────────
    if getattr(cfg, "ENABLE_EXCEL_EXPORT", False) and _in_mode("excel_export"):
        print("\n  📊 Excel ダッシュボード CSV エクスポート中...")
        try:
            _ex = _load_local_module(
                "excel_dashboard_exporter",
                "/content/excel_dashboard_exporter.py",
                cfg.DRIVE_BASE / "excel_dashboard_exporter.py",
            )
            if _ex:
                export_result = _ex.run_excel_exporter(cfg, results)
                results["excel_export"] = export_result
                logger.add("excel_export", "success",
                           f"{export_result.get('row_count',0)}行出力 "
                           f"JP:{export_result['breakdown'].get('JP',0)} "
                           f"US:{export_result['breakdown'].get('US',0)} "
                           f"ETF:{export_result['breakdown'].get('ETF',0)}")
            else:
                print("  ⚠️  excel_dashboard_exporter.py が見つかりません")
        except Exception as e:
            logger.add("excel_export", "error", str(e)[:100])
            print(f"  ⚠️  Excelエクスポートエラー: {str(e)[:80]}")
    else:
        if not getattr(cfg, "ENABLE_EXCEL_EXPORT", False):
            print("  ⏩ Excelエクスポート: スキップ（ENABLE_EXCEL_EXPORT=False）")

    # ── スコア履歴・スクリーナー履歴の蓄積（Excel出力の有無に関わらず毎回実行）──
    try:
        if results.get("sectors"):
            _append_score_history(cfg, results["sectors"])
        if results.get("screener"):
            # performance_tracker 版に一本化（_append_screener_history は C-1 で削除）
            _pt_hist = _load_local_module(
                "performance_tracker",
                "/content/performance_tracker.py",
                cfg.DRIVE_BASE / "performance_tracker.py",
            )
            if _pt_hist:
                _pt_hist.append_screener_history(cfg, results["screener"])
    except Exception as e:
        print(f"  ⚠️  履歴蓄積エラー（スキップ）: {str(e)[:60]}")
    # ── [4-9] 統合シグナルアグリゲーター ─────────────────────────
    if getattr(cfg, "ENABLE_SIGNAL_AGGREGATOR", True) and _in_mode("signal_aggregator"):
        try:
            _sa_mod = _load_local_module(
                "signal_aggregator",
                "/content/signal_aggregator.py",
                cfg.DRIVE_BASE / "signal_aggregator.py",
            )
            if _sa_mod:
                sa_result = _sa_mod.run_signal_aggregator(cfg, results)
                results["signal_aggregator"] = sa_result
                if not sa_result.get("skipped"):
                    top = sa_result.get("top_buy", [])
                    regime = sa_result.get("market_regime", "不明")
                    logger.add("signal_aggregator", "success",
                               f"レジーム:{regime} / 買い推奨:{len(top)}業種")
            else:
                print("  ⚠️  signal_aggregator.py が見つかりません")
        except Exception as e:
            logger.add("signal_aggregator", "error", str(e)[:100])
            print(f"  ⚠️  シグナルアグリゲーターエラー: {str(e)[:80]}")
    else:
        print("  ⏩ シグナルアグリゲーター: スキップ（ENABLE_SIGNAL_AGGREGATOR=False）")
    # ── [5人統合エンジン] total_signal_score 算出 ───────────
    if _in_mode("signal_aggregator"):
        try:
            _sagg = _load_local_module("signal_aggregator", ...)
            if _sagg and hasattr(_sagg, "calc_total_signal_score"):
                tss = _sagg.calc_total_signal_score(results, cfg)
                results["total_signal_score"] = tss
                print(f"\n  🧠 統合シグナルスコア: "
                      f"{tss['total_signal_score']:.0f}点 "
                      f"| {tss['signal_label']} "
                      f"| 推奨: {tss['recommended_action']}")
        except Exception as e:
            print(f"  ⚠️  統合スコア算出エラー: {e}")
    # ── [4-10] 推論エンジン ───────────────────────────────────────
    if getattr(cfg, "ENABLE_INFERENCE_ENGINE", True) and _in_mode("inference"):
        try:
            _ie_mod = _load_local_module(
                "inference_engine",
                "/content/inference_engine.py",
                cfg.DRIVE_BASE / "inference_engine.py",
            )
            if _ie_mod:
                ie_result = _ie_mod.run_inference_engine(cfg, results)
                results["inference"] = ie_result
                phase = ie_result.get("phase_label", "不明")
                n_scen = len(ie_result.get("active_scenarios", []))
                logger.add("inference_engine", "success",
                           f"フェーズ:{phase} / シナリオ:{n_scen}件")
            else:
                print("  ⚠️  inference_engine.py が見つかりません")
        except Exception as e:
            logger.add("inference_engine", "error", str(e)[:100])
            print(f"  ⚠️  推論エンジンエラー: {str(e)[:80]}")
    else:
        print("  ⏩ 推論エンジン: スキップ（ENABLE_INFERENCE_ENGINE=False）")
     # ── [6-4] Notion / Obsidian 書き込み ──────────────────────────
    if getattr(cfg, "ENABLE_NOTION_OBSIDIAN", True) and _in_mode("notion_obsidian"):
        try:
            _now = _load_local_module(
                "notion_obsidian_writer",
                "/content/notion_obsidian_writer.py",
                cfg.DRIVE_BASE / "notion_obsidian_writer.py",
            )
            if _now:
                now_result = _now.run_notion_obsidian_writer(cfg, results)
                results["notion_obsidian"] = now_result
                obs = now_result.get("obsidian", {})
                ntf = now_result.get("notion", {})
                logger.add("notion_obsidian", "success",
                           f"Obsidian:{len(obs.get('tickers',[]))}件 "
                           f"Notion:{ntf.get('success',0)}件")
        except Exception as e:
            logger.add("notion_obsidian", "error", str(e)[:100])
            print(f"  ⚠️  Notion/Obsidian書き込みエラー: {str(e)[:80]}")
    else:
        print("  ⏩ Notion/Obsidian: スキップ（ENABLE_NOTION_OBSIDIAN=False）")
    
    # 全タスク完了ログ（全モジュール実行後に出力）
    elapsed = (datetime.now() - start).seconds
    print(f"\n{'='*60}")
    print(f"  ✅ 全タスク完了（{elapsed}秒）")
    print(f"  📁 結果: {cfg.RESULT_DIR}")
    print(f"  📝 知見: {cfg.INSIGHT_DIR}")
    print(f"{'='*60}")
    logger.tail(n=5)

    return results


# ==============================================================
# BLOCK 7: Streamlit UI
# ==============================================================
# 【v2.0 との差分】このブロック全体が新規追加
#   タブ構成:
#     Tab1: マクロ概況（逆風・追い風判定）
#     Tab2: セクター分析（2-C: サマリーチャート・比較グラフ・EDINET リスク追加）
#     Tab3: ArXiv 論文（レポートビューア）
#     Tab4: 実行ログ
#
# 【v3.2 変更点（2-C）】
#   - _render_sector_overview_chart() 追加: 全実装業種のスコア棒グラフ
#   - _render_fin_chart()             追加: 業種内企業比較グラフ（plotly）
#   - Tab2 expander 内に「テーブル / グラフ」切り替えタブを追加
#   - Tab2 expander 内に EDINET リスクセクションを追加


def _render_sector_overview_chart(sector_results: dict, cfg) -> None:
    """
    実装済み業種の総合スコアを横棒グラフで表示する。
    sector_results: {code: result_dict} の session_state キャッシュ。
    スコアがない業種はグラフに含めない。
    """
    try:
        import plotly.graph_objects as go
        import streamlit as st
    except ImportError:
        return

    if not sector_results:
        return

    rows = []
    for code, res in sector_results.items():
        score = res.get("composite_score")
        if score is None:
            continue
        risk  = res.get("risk_level", "medium")
        name  = cfg.JAPAN_SECTORS.get(code, {}).get("name_ja", code)
        color = {"low": "#22c55e", "medium": "#eab308", "high": "#ef4444"}.get(risk, "#94a3b8")
        rows.append({"name": name, "score": score, "color": color, "risk": risk})

    if not rows:
        return

    rows.sort(key=lambda r: r["score"], reverse=True)
    names  = [r["name"]  for r in rows]
    scores = [r["score"] for r in rows]
    colors = [r["color"] for r in rows]

    fig = go.Figure(go.Bar(
        x=scores, y=names,
        orientation="h",
        marker_color=colors,
        text=[f"{s:.0f}点" for s in scores],
        textposition="outside",
        hovertemplate="%{y}: %{x:.1f}点<extra></extra>",
    ))
    fig.update_layout(
        title="業種別 総合スコア（💚低 / 🟡中 / 🔴高リスク）",
        xaxis_title="総合スコア",
        height=max(250, 40 * len(rows)),
        margin=dict(l=10, r=60, t=40, b=10),
        xaxis=dict(range=[min(0, min(scores) - 5), max(scores) + 15]),
    )
    st.plotly_chart(fig, use_container_width=True)


def _render_fin_chart(companies: list, sector_name: str) -> None:
    """
    業種内の企業財務指標（ROE / ROA / 営業利益率）を
    グループ棒グラフで表示する。
    companies: top_companies リスト（fetch_ok=True の要素を対象）。
    """
    try:
        import plotly.graph_objects as go
        import streamlit as st
    except ImportError:
        return

    ok = [c for c in companies if c.get("fetch_ok")]
    if not ok:
        st.caption("財務データがないためグラフを表示できません")
        return

    names   = [c.get("name", c["ticker"])[:12] for c in ok]
    roe_pct = [round(c["roe"] * 100, 1) if c.get("roe") is not None else None for c in ok]
    roa_pct = [round(c["roa"] * 100, 1) if c.get("roa") is not None else None for c in ok]
    opm_pct = [round(c["op_margin"] * 100, 1) if c.get("op_margin") is not None else None for c in ok]

    # None を 0 に置換（グラフ描画用）
    def _fill(lst):
        return [v if v is not None else 0 for v in lst]

    fig = go.Figure()
    fig.add_trace(go.Bar(name="ROE (%)",   x=names, y=_fill(roe_pct),
                         marker_color="#3b82f6",
                         text=[f"{v}%" if v else "N/A" for v in roe_pct],
                         textposition="outside"))
    fig.add_trace(go.Bar(name="ROA (%)",   x=names, y=_fill(roa_pct),
                         marker_color="#10b981",
                         text=[f"{v}%" if v else "N/A" for v in roa_pct],
                         textposition="outside"))
    fig.add_trace(go.Bar(name="営業利益率 (%)", x=names, y=_fill(opm_pct),
                         marker_color="#f59e0b",
                         text=[f"{v}%" if v else "N/A" for v in opm_pct],
                         textposition="outside"))
    fig.update_layout(
        title=f"{sector_name} — 企業財務指標比較",
        barmode="group",
        yaxis_title="(%)",
        height=350,
        margin=dict(l=10, r=10, t=40, b=10),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    # ROE 10% / ROA 5% の基準線を追加
    fig.add_hline(y=10, line_dash="dot", line_color="#3b82f6",
                  annotation_text="ROE 10%基準", annotation_position="right")
    fig.add_hline(y=5,  line_dash="dot", line_color="#10b981",
                  annotation_text="ROA 5%基準",  annotation_position="right")
    st.plotly_chart(fig, use_container_width=True)


def run_streamlit_ui():
    """Streamlit UI を起動する（colab_analyzer_start_v3.py から呼ばれる）"""
    try:
        import streamlit as st
    except ImportError:
        print("  ❌ streamlit が未インストールです")
        return

    st.set_page_config(
        page_title="自律型投資AIエージェント",
        page_icon="🤖",
        layout="wide",
    )

    # ── CSS ─────────────────────────────────────────────────────
    st.markdown("""
    <style>
    .sig-tailwind { color: #22c55e; font-weight: 700; }
    .sig-headwind { color: #ef4444; font-weight: 700; }
    .sig-watch    { color: #f59e0b; font-weight: 700; }
    </style>
    """, unsafe_allow_html=True)

    st.title("🤖 自律型投資AIエージェント基盤 v1.0（Step 1）")
    st.caption("config.py のルールに基づきマクロ × 33業種の追い風/逆風を自動判定")

    tab1, tab2, tab3, tab4 = st.tabs(
        ["📈 マクロ概況", "🏭 セクター分析", "📚 ArXiv論文", "📋 実行ログ"]
    )

    # ── Tab1: マクロ概況 ─────────────────────────────────────────
    # ── Tab1: マクロ概況（v3.3: 長期/短期タブ追加） ─────────────
    with tab1:
        st.subheader("マクロ指標 × 業種シグナル")

        # ── 操作ボタン行（3列）──────────────────────────────────
        btn_col1, btn_col2, btn_col3 = st.columns(3)
        with btn_col1:
            if st.button("🔄 マクロ指標を取得・更新", key="tab1_macro_update"):
                with st.spinner("yfinance から取得中..."):
                    snapshot = fetch_macro_snapshot()
                    signals  = evaluate_macro_signals(snapshot)
                    st.session_state["macro_snapshot"] = snapshot
                    st.session_state["macro_signals"]  = signals
                st.success("更新完了")
        with btn_col2:
            if st.button("⏳ タイムライン分析（長期＋短期）", key="tab1_timeline_run"):
                with st.spinner("株価データ取得中（数分かかります）..."):
                    try:
                        _tm = _load_local_module(
                            "timeline_manager",
                            "/content/timeline_manager.py",
                            cfg.DRIVE_BASE / "timeline_manager.py",
                        )
                        if _tm:
                            tl = _tm.run_timeline_analysis(cfg)
                            st.session_state["timeline_result"] = tl
                            st.success("タイムライン分析完了")
                        else:
                            st.error("timeline_manager.py が見つかりません")
                    except Exception as e:
                        st.error(f"エラー: {e}")
        with btn_col3:
            if st.button("🏦 政策金利トラッカー実行", key="tab1_policy_run"):
                with st.spinner("レートデータ取得・変化検知中..."):
                    try:
                        _pt = _load_local_module(
                            "policy_tracker",
                            "/content/policy_tracker.py",
                            cfg.DRIVE_BASE / "policy_tracker.py",
                        )
                        if _pt:
                            pt = _pt.run_policy_tracker(cfg)
                            st.session_state["policy_result"] = pt
                            n = len(pt.get("new_events", []))
                            if n:
                                st.warning(f"⚡ 完了（新規イベント: {n}件）")
                            else:
                                st.success("完了（有意な変化なし）")
                        else:
                            st.error("policy_tracker.py が見つかりません")
                    except Exception as e:
                        st.error(f"エラー: {e}")

        # ── マクロ指標メトリクス（常時表示）──────────────────────
        snapshot = st.session_state.get("macro_snapshot", {})
        signals  = st.session_state.get("macro_signals", [])

        if snapshot:
            labels = {
                "usdjpy":  "USD/JPY",
                "us10y":   "米10年債(%)",
                "oil_wti": "WTI原油($)",
                "vix":     "VIX",
                "nikkei":  "日経平均",
                "sp500":   "S&P500",
            }
            cols = st.columns(3)
            for i, (k, label) in enumerate(labels.items()):
                v = snapshot.get(k)
                with cols[i % 3]:
                    st.metric(label, f"{v:.3g}" if v else "N/A")

        st.markdown("---")

        # ── 内部4タブ（3-C で 3→4 に拡張）──────────────────────
        inner1, inner2, inner3, inner4 = st.tabs([
            "🌐 マクロシグナル",
            "📈 長期トレンド（月次）",
            "⚡ 短期デイリー（前日比）",
            "📡 政策金利トラッカー",
        ])

        # inner1: マクロシグナル（既存ロジック）
        with inner1:
            if signals:
                st.markdown(f"**アクティブなシグナル: {len(signals)}件**")
                for s in signals:
                    with st.expander(
                        f"**{s['label']}** — {s['trigger_value']}", expanded=True
                    ):
                        rows = []
                        for code, sig in s["sector_signals"].items():
                            name   = cfg.JAPAN_SECTORS.get(code, {}).get("name_ja", code)
                            icon   = {
                                "tailwind": "💚 追い風",
                                "headwind": "🔴 逆風",
                                "watch":    "🟡 注意",
                            }.get(sig, "⚪")
                            reason = s["rationale"].get(code, "")
                            rows.append({"業種": name, "判定": icon, "理由": reason})
                        st.dataframe(
                            pd.DataFrame(rows),
                            use_container_width=True,
                            hide_index=True,
                        )
            else:
                st.info(
                    "「マクロ指標を取得・更新」ボタンを押してシグナルを表示してください。\n"
                    "シグナルは config.py の MACRO_RULES に基づいて自動判定されます。"
                )

        # inner2: 長期トレンド（月次移動平均）
        with inner2:
            tl_result = st.session_state.get("timeline_result", {})
            long_term = tl_result.get("long_term", {}) if tl_result else {}

            if not long_term or not long_term.get("sector_trends"):
                st.info(
                    f"「タイムライン分析を実行」ボタンを押すと、\n"
                    f"業種代表銘柄の {getattr(cfg,'LONG_TERM_MONTHS',12)}ヶ月/"
                    f"MA{getattr(cfg,'LONG_TERM_MA_WINDOW',3)}ヶ月トレンドを表示します。"
                )
            else:
                sector_trends = long_term.get("sector_trends", [])
                golden        = long_term.get("golden_cross_sectors", [])
                dead          = long_term.get("dead_cross_sectors",   [])

                # サマリーメトリクス
                mc1, mc2, mc3, mc4 = st.columns(4)
                up_cnt   = sum(1 for s in sector_trends if s["trend_summary"] == "up")
                down_cnt = sum(1 for s in sector_trends if s["trend_summary"] == "down")
                mc1.metric("分析業種数",       len(sector_trends))
                mc2.metric("📈 上昇トレンド",  up_cnt)
                mc3.metric("📉 下落トレンド",  down_cnt)
                mc4.metric("⚡ 転換点（今月）", len(golden) + len(dead))

                # 転換点検出業種を優先表示
                crossed = [s for s in sector_trends if s.get("cross_flags")]
                if crossed:
                    st.markdown("#### ⚡ 今月の転換点業種")
                    for s in crossed:
                        icon = "📈" if s["trend_summary"] == "up" else "📉"
                        with st.expander(
                            f"{icon} **{s['sector_name']}** `{s['sector_code']}`",
                            expanded=True,
                        ):
                            for flag in s.get("cross_flags", []):
                                st.markdown(f"- {flag}")
                            # 銘柄詳細テーブル
                            det_rows = []
                            for tkr, d in s.get("ticker_details", {}).items():
                                t_icon = {"up": "📈", "down": "📉", "flat": "➡️"}.get(
                                    d["trend"], "⚪")
                                det_rows.append({
                                    "銘柄":    tkr,
                                    "現在値":  f"¥{d['latest_price']:,.0f}",
                                    f"MA{getattr(cfg,'LONG_TERM_MA_WINDOW',3)}ヶ月": f"¥{d['latest_ma']:,.0f}",
                                    "乖離率":  f"{d['deviation_pct']:+.1f}%",
                                    "方向":    t_icon,
                                })
                            if det_rows:
                                st.dataframe(
                                    pd.DataFrame(det_rows),
                                    use_container_width=True,
                                    hide_index=True,
                                )
                    st.markdown("---")

                # 全業種サマリーテーブル
                st.markdown(f"#### 全業種 トレンドサマリー（{long_term.get('analysis_month','')}）")
                all_rows = []
                for s in sector_trends:
                    t_icon = {"up": "📈 上昇", "down": "📉 下落",
                              "flat": "➡️ 横ばい"}.get(s["trend_summary"], "⚪")
                    cross_str = " | ".join(s.get("cross_flags", [])) or "—"
                    all_rows.append({
                        "業種":       s["sector_name"],
                        "コード":     s["sector_code"],
                        "トレンド":   t_icon,
                        "転換フラグ": cross_str[:60],
                    })
                if all_rows:
                    st.dataframe(
                        pd.DataFrame(all_rows),
                        use_container_width=True,
                        hide_index=True,
                    )

                # 最新レポートリンク
                if cfg.MACRO_DIR.exists():
                    rpts = sorted(cfg.MACRO_DIR.glob("monthly_trend_*.md"), reverse=True)
                    if rpts:
                        st.caption(f"📄 最新レポート: {rpts[0].name}")

        # inner3: 短期デイリー（前日比ランキング）
        with inner3:
            tl_result  = st.session_state.get("timeline_result", {})
            short_term = tl_result.get("short_term", []) if tl_result else []

            if not short_term:
                st.info(
                    "「タイムライン分析を実行」ボタンを押すと、\n"
                    "業種代表銘柄の前日比変化率ランキングを表示します。"
                )
            else:
                top_n = getattr(cfg, "SHORT_TERM_TOP_N", 5)
                ts    = tl_result.get("timestamp", "")
                st.caption(f"取得時刻: {ts}")

                # 上昇/下落を横並び
                col_up, col_dn = st.columns(2)

                with col_up:
                    st.markdown(f"#### 📈 上昇 TOP {top_n}")
                    rising = [s for s in short_term if s["avg_change_pct"] > 0][:top_n]
                    if rising:
                        st.dataframe(
                            pd.DataFrame([{
                                "順位": i + 1,
                                "業種": s["sector_name"],
                                "騰落率": f"{s['avg_change_pct']:+.2f}%",
                                "ベスト": f"{s['best_ticker']} ({s['best_change']:+.2f}%)",
                            } for i, s in enumerate(rising)]),
                            use_container_width=True,
                            hide_index=True,
                        )
                    else:
                        st.caption("上昇業種なし")

                with col_dn:
                    st.markdown(f"#### 📉 下落 TOP {top_n}")
                    falling = [s for s in reversed(short_term) if s["avg_change_pct"] < 0][:top_n]
                    if falling:
                        st.dataframe(
                            pd.DataFrame([{
                                "順位": i + 1,
                                "業種": s["sector_name"],
                                "騰落率": f"{s['avg_change_pct']:+.2f}%",
                                "ワースト": f"{s['worst_ticker']} ({s['worst_change']:+.2f}%)",
                            } for i, s in enumerate(falling)]),
                            use_container_width=True,
                            hide_index=True,
                        )
                    else:
                        st.caption("下落業種なし")

                # 全業種ランキング（折りたたみ）
                with st.expander("📋 全業種ランキング一覧"):
                    st.dataframe(
                        pd.DataFrame([{
                            "順位": i + 1,
                            "業種": s["sector_name"],
                            "平均騰落率": f"{s['avg_change_pct']:+.2f}%",
                            "方向": "📈" if s["avg_change_pct"] >= 0 else "📉",
                            "データ銘柄数": s["ticker_count"],
                        } for i, s in enumerate(short_term)]),
                        use_container_width=True,
                        hide_index=True,
                    )

                # 最新レポートリンク
                if cfg.MACRO_DIR.exists():
                    rpts = sorted(cfg.MACRO_DIR.glob("daily_movers_*.md"), reverse=True)
                    if rpts:
                        st.caption(f"📄 最新レポート: {rpts[0].name}")

        # ── inner4: 政策金利トラッカー（3-C 追加）────────────────
        with inner4:
            pt_result = st.session_state.get("policy_result", {})

            if not pt_result:
                st.info(
                    "「🏦 政策金利トラッカー実行」ボタンを押すと、\n"
                    "米10年債・日本10年債・FFレートプロキシ(^IRX)・USD/JPY を取得し、\n"
                    "前回実行時からの変化を自動検出して表示します。"
                )
            else:
                new_events   = pt_result.get("new_events",    [])
                all_events   = pt_result.get("all_events",    [])
                current_rates= pt_result.get("current_rates", {})
                rate_history = pt_result.get("rate_history",  {})
                fred_data    = pt_result.get("fred_data",     {})
                ts           = pt_result.get("timestamp",     "")
                threshold    = getattr(cfg, "POLICY_CHANGE_THRESHOLD", 0.05)

                st.caption(f"取得時刻: {ts}  |  変化閾値: ±{threshold}%pt")

                # ── 新規イベントバナー ────────────────────────
                if new_events:
                    st.error(f"⚡ **政策金利変化を{len(new_events)}件検出しました**")
                    for ev in new_events:
                        chg = ev.get("change_pt", 0)
                        icon = "🔴" if abs(chg) >= 0.25 else ("🟡" if abs(chg) >= 0.10 else "⚪")
                        st.markdown(
                            f"{icon} **[{ev.get('bank','')}] {ev.get('label','')}** "
                            f"`{ev.get('prev_value',0):.3f}` → `{ev.get('curr_value',0):.3f}` "
                            f"（{chg:+.3f}%pt）{ev.get('direction','')}"
                        )
                        if ev.get("sector_impact"):
                            st.caption("　業種影響: " + " / ".join(ev["sector_impact"]))
                else:
                    st.success("✅ 有意な変化なし（閾値以上の変動は検出されませんでした）")

                st.markdown("---")

                # ── 現在レートスナップショット ────────────────
                st.markdown("#### 📊 現在レート")
                if current_rates:
                    snap_rows = []
                    for ticker, info in current_rates.items():
                        snap_rows.append({
                            "指標":   info.get("label", ticker),
                            "機関":   info.get("bank",  ""),
                            "現在値": f"{info.get('value', 0):.3f}",
                            "基準日": info.get("date", ""),
                        })
                    st.dataframe(
                        pd.DataFrame(snap_rows),
                        use_container_width=True,
                        hide_index=True,
                    )

                # ── 直近推移チャート ──────────────────────────
                if rate_history:
                    st.markdown("---")
                    st.markdown("#### 📈 直近30日 レート推移")
                    try:
                        import plotly.graph_objects as go
                        TICKER_LABELS = {
                            "^IRX":        "米FFレートproxy(^IRX)",
                            "^TNX":        "米10年債(^TNX)",
                            "^JP10YT=RR":  "日本10年債",
                            "JPY=X":       "USD/JPY",
                        }
                        # USD/JPY は右軸へ分離
                        fig = go.Figure()
                        for ticker, records in rate_history.items():
                            if not records:
                                continue
                            label = TICKER_LABELS.get(ticker, ticker)
                            dates  = [r["date"]  for r in records]
                            values = [r["value"] for r in records]
                            yaxis  = "y2" if ticker == "JPY=X" else "y"
                            fig.add_trace(go.Scatter(
                                x=dates, y=values,
                                mode="lines",
                                name=label,
                                yaxis=yaxis,
                                line=dict(width=2),
                            ))
                        fig.update_layout(
                            height=320,
                            margin=dict(l=0, r=60, t=20, b=0),
                            legend=dict(orientation="h", yanchor="bottom", y=1.02),
                            yaxis=dict(title="利回り(%)"),
                            yaxis2=dict(
                                title="USD/JPY",
                                overlaying="y",
                                side="right",
                                showgrid=False,
                            ),
                        )
                        st.plotly_chart(fig, use_container_width=True)
                    except ImportError:
                        # plotly なし → テーブル表示
                        rows = []
                        for ticker, records in rate_history.items():
                            if records:
                                rows.append({
                                    "指標":  ticker,
                                    "最新値": f"{records[-1]['value']:.3f}",
                                    "1ヶ月前": f"{records[0]['value']:.3f}",
                                    "変化量": f"{records[-1]['value'] - records[0]['value']:+.3f}",
                                })
                        if rows:
                            st.dataframe(pd.DataFrame(rows),
                                         use_container_width=True, hide_index=True)

                # ── FRED 実績値（取得できた場合のみ）────────────
                if fred_data:
                    st.markdown("---")
                    st.markdown("#### 🏛️ FRED 実績値（FFレート等）")
                    for sid, records in fred_data.items():
                        if records:
                            last = records[-1]
                            st.metric(sid, f"{last['value']:.2f}%",
                                      delta=None,
                                      help=f"基準日: {last['date']}")

                # ── 累積イベント履歴 ──────────────────────────
                if all_events:
                    st.markdown("---")
                    st.markdown("#### 📋 変化イベント履歴（直近20件）")
                    disp_rows = []
                    for ev in reversed(all_events[-20:]):
                        chg = ev.get("change_pt", 0)
                        disp_rows.append({
                            "検知日時": ev.get("detected_at", "")[:16],
                            "機関":     ev.get("bank", ""),
                            "指標":     ev.get("label", "")[:22],
                            "変化量":   f"{chg:+.3f}%pt",
                            "方向":     ev.get("direction", "")[:20],
                        })
                    st.dataframe(
                        pd.DataFrame(disp_rows),
                        use_container_width=True,
                        hide_index=True,
                    )

                # FRED API 設定ガイド
                if not getattr(cfg, "POLICY_FRED_API_KEY", ""):
                    with st.expander("💡 FRED API を設定すると FFレート実績値も取得できます"):
                        st.markdown(
                            "1. https://fred.stlouisfed.org/docs/api/api_key.html で無料登録\n"
                            "2. 取得したキーを `config.py` の `POLICY_FRED_API_KEY` に貼り付ける\n"
                            "3. FFレート(FEDFUNDS)・超過準備金利(IORB) の実績値が自動取得されます"
                        )

                # 最新レポートリンク
                if cfg.MACRO_DIR.exists():
                    rpts = sorted(cfg.MACRO_DIR.glob("policy_changes_*.md"), reverse=True)
                    if rpts:
                        st.caption(f"📄 最新レポート: {rpts[0].name}")

    # ── Tab2: セクター分析（v3.2: サマリーチャート・比較グラフ・EDINET リスク） ──
    with tab2:
        st.subheader("33業種 セクター分析")

        snapshot       = st.session_state.get("macro_snapshot", {})
        signals        = st.session_state.get("macro_signals", [])
        sector_results = st.session_state.get("sector_results", {})

        # ── セクターサマリーチャート（分析済み業種があれば表示）───
        if sector_results:
            with st.expander("📊 業種別スコア サマリーチャート", expanded=True):
                _render_sector_overview_chart(sector_results, cfg)
            st.markdown("---")

        # ── フィルタ & 表示設定 ──────────────────────────────────
        col_f1, col_f2, col_f3, col_f4 = st.columns([3, 3, 2, 2])
        with col_f1:
            group_filter = st.selectbox(
                "業種グループ",
                ["すべて"] + cfg.SECTOR_GROUPS,
            )
        with col_f2:
            show_mode = st.radio(
                "表示モード",
                ["一覧（全業種）", "実装済みのみ"],
                horizontal=True,
            )
        with col_f3:
            sort_mode = st.radio(
                "並び順",
                ["デフォルト", "スコア順"],
                horizontal=True,
            )
        with col_f4:
            st.metric("実装済みエージェント", f"{len(cfg.IMPLEMENTED_AGENTS)} / 33")

        # ── 全業種一括分析ボタン ──────────────────────────────────
        col_b1, col_b2 = st.columns([2, 5])
        with col_b1:
            if cfg.IMPLEMENTED_AGENTS and st.button(
                f"▶ 全{len(cfg.IMPLEMENTED_AGENTS)}業種を一括分析", key="tab2_run_all"
            ):
                prog = st.progress(0, text="分析中...")
                cached = st.session_state.get("sector_results", {})
                for i, code in enumerate(cfg.IMPLEMENTED_AGENTS):
                    name = cfg.JAPAN_SECTORS.get(code, {}).get("name_ja", code)
                    prog.progress((i + 1) / len(cfg.IMPLEMENTED_AGENTS),
                                  text=f"{name} 分析中... ({i+1}/{len(cfg.IMPLEMENTED_AGENTS)})")
                    r = run_sector_analysis(code, snapshot)
                    cached[code] = r
                st.session_state["sector_results"] = cached
                prog.empty()
                st.success(f"{len(cfg.IMPLEMENTED_AGENTS)}業種の分析が完了しました")
                st.rerun()

        # ── 個別業種の即時分析 ────────────────────────────────────
        with col_b2:
            if cfg.IMPLEMENTED_AGENTS:
                with st.expander("🔍 1業種だけ選択して分析"):
                    sel_code = st.selectbox(
                        "業種を選択",
                        cfg.IMPLEMENTED_AGENTS,
                        format_func=lambda c: f"{c} {cfg.JAPAN_SECTORS.get(c,{}).get('name_ja',c)}",
                        key="tab2_manual_sector",
                    )
                    if st.button("▶ この業種を今すぐ分析", key="tab2_run_one"):
                        with st.spinner(f"{cfg.JAPAN_SECTORS.get(sel_code,{}).get('name_ja',sel_code)} 分析中..."):
                            res = run_sector_analysis(sel_code, snapshot)
                            cached = st.session_state.get("sector_results", {})
                            cached[sel_code] = res
                            st.session_state["sector_results"] = cached
                        st.success("分析完了")
                        st.rerun()

        st.markdown("---")

        # ── 業種リスト整形（並び順適用）────────────────────────
        sector_items = list(cfg.JAPAN_SECTORS.items())
        if sort_mode == "スコア順" and sector_results:
            def _score_key(item):
                res = sector_results.get(item[0])
                return -(res.get("composite_score", -999) if res else -999)
            sector_items.sort(key=_score_key)

        # ── 業種カード表示 ───────────────────────────────────────
        for code, info in sector_items:
            if group_filter != "すべて" and info["group"] != group_filter:
                continue

            implemented = code in cfg.IMPLEMENTED_AGENTS
            if show_mode == "実装済みのみ" and not implemented:
                continue

            macro_icon = get_sector_macro_icon(code, signals) if signals else "⚪"
            res        = sector_results.get(code)

            # ── 実装済み & 分析済み: フル詳細カード ─────────────
            if implemented and res:
                score     = res.get("composite_score", "N/A")
                risk      = res.get("risk_level", "N/A")
                risk_icon = {"high": "🔴", "medium": "🟡", "low": "💚"}.get(risk, "⚪")
                companies = res.get("top_companies", [])
                ms        = res.get("macro_signals", [])
                e_risks   = res.get("edinet_risks", [])

                with st.expander(
                    f"{macro_icon} **{info['name_ja']}** `{code}` "
                    f"— スコア: {score}点 {risk_icon}",
                    expanded=False,
                ):
                    # ── マクロシグナル ───────────────────────────
                    if ms:
                        sig_cols = st.columns(min(len(ms), 4))
                        for i, s in enumerate(ms[:4]):
                            sig_icon = {
                                "tailwind": "💚 追い風",
                                "headwind": "🔴 逆風",
                                "watch":    "🟡 注意",
                            }.get(s["signal"], "⚪")
                            with sig_cols[i]:
                                st.markdown(f"**{s['label']}**")
                                st.caption(sig_icon)
                                st.caption(s.get("rationale", "")[:55])
                    st.markdown("---")

                    # ── 財務指標: テーブル / グラフ切り替えタブ ──
                    ok_cos = [c for c in companies if c.get("fetch_ok")]
                    if ok_cos:
                        fin_tab1, fin_tab2 = st.tabs(["📋 テーブル", "📊 グラフ"])

                        with fin_tab1:
                            rows = []
                            for c in ok_cos:
                                def _p(v): return f"{v*100:.1f}%" if v is not None else "N/A"
                                def _v(v): return f"{v:.2f}" if v is not None else "N/A"
                                try:
                                    from sector_agents.base_agent import fmt_cap
                                    cap_str = fmt_cap(c.get("market_cap"))
                                except Exception:
                                    cap_str = "N/A"
                                rows.append({
                                    "銘柄":       c.get("name", c["ticker"])[:18],
                                    "時価総額":   cap_str,
                                    "ROE":        _p(c.get("roe")),
                                    "ROA":        _p(c.get("roa")),
                                    "営業利益率": _p(c.get("op_margin")),
                                    "PBR":        f"{_v(c.get('pbr'))}x",
                                    "PER":        f"{_v(c.get('per'))}x",
                                    "52週騰落":   _p(c.get("week52_change")),
                                })
                            st.dataframe(
                                pd.DataFrame(rows),
                                use_container_width=True,
                                hide_index=True,
                            )

                        with fin_tab2:
                            _render_fin_chart(ok_cos, info["name_ja"])

                    else:
                        st.caption("財務データが取得できませんでした（yfinance 通信エラーの可能性）")

                    # ── EDINET リスク表示 ─────────────────────────
                    if e_risks:
                        st.markdown("---")
                        st.markdown("### 📄 有報「事業のリスク」（EDINET 自動取得）")
                        risk_by_ticker: dict = {}
                        for item in e_risks:
                            t = item.get("ticker", "")
                            risk_by_ticker.setdefault(t, []).append(item.get("risk", ""))
                        ticker_to_name = {c["ticker"]: c.get("name", c["ticker"])
                                          for c in companies if c.get("fetch_ok")}
                        for ticker, risk_list in risk_by_ticker.items():
                            name = ticker_to_name.get(ticker, ticker)
                            st.markdown(f"**{name}** `{ticker}`")
                            for risk_item in risk_list:
                                st.markdown(f"- {risk_item}")

                    # ── 業種特化ノート ───────────────────────────
                    note = res.get("sector_note", "")
                    if note:
                        st.markdown("---")
                        st.markdown(note)

            # ── 実装済みだが未分析: 簡易カード + 分析ボタン ─────
            elif implemented and not res:
                col1, col2, col3 = st.columns([1, 6, 2])
                with col1:
                    st.markdown(f"## {macro_icon}")
                with col2:
                    st.markdown(f"**{info['name_ja']}** `{code}` 🤖")
                    st.caption(f"{info.get('name_en','')} | {info['group']}")
                with col3:
                    if st.button("▶ 分析", key=f"quick_{code}"):
                        with st.spinner("分析中..."):
                            r = run_sector_analysis(code, snapshot)
                            cached = st.session_state.get("sector_results", {})
                            cached[code] = r
                            st.session_state["sector_results"] = cached
                        st.rerun()
                st.divider()

            # ── 未実装業種: 一覧のみ ─────────────────────────────
            else:
                col1, col2, col3 = st.columns([1, 6, 2])
                with col1:
                    st.markdown(f"## {macro_icon}")
                with col2:
                    st.markdown(f"**{info['name_ja']}** `{code}`")
                    st.caption(f"{info.get('name_en','')} | {info['group']}")
                with col3:
                    st.caption("📋 基本シグナルのみ")
                    reps = cfg.SECTOR_REPRESENTATIVE_TICKERS.get(code, [])
                    if reps:
                        st.caption(", ".join(reps[:2]))
                st.divider()

# ── Tab3: ArXiv 論文 × 投資アイデア（4-A 拡充） ──────────────
    with tab3:
        st.subheader("💡 投資アイデア × ArXiv 論文")

        # ── アイデア生成ボタン ────────────────────────────────────
        col_btn1, col_btn2 = st.columns([3, 4])
        with col_btn1:
            if st.button("💡 アイデアを今すぐ生成", key="tab3_idea_gen"):
                with st.spinner("ArXiv × マクロ × セクターを照合中..."):
                    try:
                        _ig = _load_local_module(
                            "idea_generator",
                            "/content/idea_generator.py",
                            cfg.DRIVE_BASE / "idea_generator.py",
                        )
                        if _ig:
                            ig = _ig.run_idea_generator(
                                cfg,
                                sector_results = st.session_state.get("sector_results", {}),
                                macro_signals  = st.session_state.get("macro_signals", []),
                            )
                            st.session_state["idea_result"] = ig
                            n = len(ig.get("ideas", []))
                            st.success(f"生成完了（{n}件）")
                        else:
                            st.error("idea_generator.py が見つかりません")
                    except Exception as e:
                        st.error(f"エラー: {e}")

        # ── アイデア表示 ──────────────────────────────────────────
        idea_result = st.session_state.get("idea_result")
        if idea_result is None:
            try:
                _ig = _load_local_module(
                    "idea_generator",
                    "/content/idea_generator.py",
                    cfg.DRIVE_BASE / "idea_generator.py",
                )
                if _ig:
                    idea_result = _ig.load_latest_ideas(cfg)
                    if idea_result:
                        st.session_state["idea_result"] = idea_result
            except Exception:
                pass

        ideas = idea_result.get("ideas", []) if idea_result else []
        meta  = idea_result.get("meta",  {}) if idea_result else {}

        if ideas:
            st.caption(
                f"生成日時: {meta.get('timestamp','')[:16]}  |  "
                f"論文: {meta.get('papers_scanned',0)}件スキャン  |  "
                f"シグナル: {meta.get('signals_active',0)}件"
            )
            st.markdown("---")
            for idea in ideas:
                score = idea.get("idea_score", 0)
                score_color = "🟢" if score >= 70 else ("🟡" if score >= 50 else "🔴")
                with st.expander(
                    f"#{idea['rank']}  {score_color} **{idea['title']}**"
                    f"  ｜ スコア: {score}/100",
                    expanded=(idea["rank"] == 1),
                ):
                    c1, c2 = st.columns([2, 1])
                    with c1:
                        st.markdown("**📌 投資テーゼ**")
                        st.info(idea.get("thesis", ""))
                        st.markdown("**📄 参照論文**")
                        url = idea.get("paper_url", "")
                        title_short = idea["source_paper"][:70]
                        if url:
                            st.markdown(f"[{title_short}]({url})")
                        else:
                            st.caption(title_short)
                    with c2:
                        st.metric("業種スコア", f"{idea.get('sector_score', 0):.0f}/100")
                        st.metric("業種", idea.get("sector_name", ""))
                        sigs = idea.get("macro_signals", [])
                        if sigs:
                            st.success("マクロ追い風: " + " / ".join(sigs))
                        else:
                            st.caption("マクロシグナル: なし")
                    st.caption("キーワード: " + ", ".join(idea.get("keywords", [])[:6]))
            st.markdown("---")
        else:
            st.info(
                "「💡 アイデアを今すぐ生成」ボタンを押すか、\n"
                "サイドバーの「▶ 全タスク実行」を押してください。"
            )
# ── アイデア評価パネル（4-B 追加）────────────────────────
        with st.expander("📋 アイデア評価・状態管理", expanded=False):
            try:
                _ie = _load_local_module(
                    "idea_evaluator",
                    "/content/idea_evaluator.py",
                    cfg.DRIVE_BASE / "idea_evaluator.py",
                )
            except Exception:
                _ie = None

            if not _ie:
                st.caption("idea_evaluator.py が見つかりません")
            else:
                STATUS_LABEL = {
                    "pending":  "⬜ 未評価",
                    "active":   "🟢 継続追跡",
                    "watching": "🟡 様子見",
                    "rejected": "🔴 棄却",
                }

                # ── 週次集計ボタン ────────────────────────────
                eval_col1, eval_col2 = st.columns([3, 4])
                with eval_col1:
                    if st.button("📊 週次集計を今すぐ実行", key="eval_weekly_btn"):
                        with st.spinner("週次集計中..."):
                            try:
                                summary = _ie.run_weekly_summary(cfg)
                                st.success(
                                    f"完了 — active:{len(summary.get('active',[]))}件 / "
                                    f"watching:{len(summary.get('watching',[]))}件"
                                )
                            except Exception as e:
                                st.error(f"エラー: {e}")

                # ── 全アイデアを状態別に表示 ──────────────────
                grouped = _ie.get_all_ideas_by_status(cfg)
                total   = sum(len(v) for v in grouped.values())

                if total == 0:
                    st.caption("評価済みアイデアがまだありません。「💡 アイデアを今すぐ生成」を先に実行してください。")
                else:
                    # 状態サマリー
                    sm1, sm2, sm3, sm4 = st.columns(4)
                    sm1.metric("⬜ 未評価",   len(grouped.get("pending",  [])))
                    sm2.metric("🟢 継続追跡", len(grouped.get("active",   [])))
                    sm3.metric("🟡 様子見",   len(grouped.get("watching", [])))
                    sm4.metric("🔴 棄却",     len(grouped.get("rejected", [])))
                    st.markdown("---")

                    # 全アイデアを1テーブルで表示
                    all_rows = []
                    for status in ("active", "watching", "pending", "rejected"):
                        for e in grouped.get(status, []):
                            all_rows.append({
                                "状態":    STATUS_LABEL.get(status, status),
                                "業種":    e.get("sector_name", ""),
                                "タイトル": e.get("title", "")[:35],
                                "スコア":  f"{e.get('idea_score', 0):.1f}",
                                "登録日":  e.get("created_at", ""),
                                "メモ":    e.get("user_note", "")[:20],
                            })
                    if all_rows:
                        st.dataframe(
                            pd.DataFrame(all_rows),
                            use_container_width=True,
                            hide_index=True,
                        )

                    # ── 状態変更フォーム ──────────────────────
                    st.markdown("#### ✏️ 状態を変更する")
                    evals = _ie.load_evaluations(cfg)
                    idea_options = {
                        eid: f"{e.get('title','')[:30]} [{STATUS_LABEL.get(e.get('status',''),'')}]"
                        for eid, e in evals.items()
                    }
                    if idea_options:
                        sel_id = st.selectbox(
                            "アイデアを選択",
                            options=list(idea_options.keys()),
                            format_func=lambda k: idea_options[k],
                            key="eval_select_idea",
                        )
                        new_status = st.radio(
                            "新しい状態",
                            options=["pending", "active", "watching", "rejected"],
                            format_func=lambda s: STATUS_LABEL[s],
                            horizontal=True,
                            key="eval_new_status",
                        )
                        note = st.text_input("メモ（任意）", key="eval_note")
                        if st.button("✅ 状態を保存", key="eval_save_btn"):
                            ok = _ie.update_status(cfg, sel_id, new_status, note)
                            if ok:
                                st.success(f"保存しました: {STATUS_LABEL[new_status]}")
                                st.rerun()
                            else:
                                st.error("保存に失敗しました")

                # ── 週次レポートリンク ────────────────────────
                idea_dir = Path(cfg.INSIGHT_DIR) / "ideas"
                if idea_dir.exists():
                    reports = sorted(idea_dir.glob("weekly_summary_*.md"), reverse=True)
                    if reports:
                        st.caption(f"📄 最新週次レポート: {reports[0].name}")

        with st.expander("📚 ArXiv 論文レポート（生データ）"):
            reports = sorted(cfg.ARXIV_DIR.glob("arxiv_report_*.md"), reverse=True)
            if not reports:
                st.caption("論文レポートがまだありません")
            else:
                selected = st.selectbox(
                    "レポートを選択",
                    [r.name for r in reports[:10]],
                    key="tab3_arxiv_select",
                )
                rp = cfg.ARXIV_DIR / selected
                try:
                    st.markdown(rp.read_text(encoding="utf-8"))
                except Exception as e:
                    st.error(f"レポート読み込み失敗: {e}")

    # ── Tab4: 実行ログ ────────────────────────────────────────────
    with tab4:
        st.subheader("実行ログ（run_history.json）")
        log_path = cfg.LOG_DIR / cfg.LOG_FILENAME
        if log_path.exists():
            try:
                logs = json.loads(log_path.read_text(encoding="utf-8"))
                df   = pd.DataFrame(logs[::-1])  # 新しい順に表示
                st.dataframe(df, use_container_width=True, hide_index=True)
            except Exception as e:
                st.error(f"ログ読み込み失敗: {e}")
        else:
            st.info("ログファイルがまだありません")

        # ArXiv 取得詳細ログ
        fetch_log_path = cfg.LOG_DIR / "arxiv_fetch_log.json"
        if fetch_log_path.exists():
            st.markdown("---")
            st.subheader("ArXiv 取得詳細ログ（arxiv_fetch_log.json）")
            try:
                fetch_logs = json.loads(fetch_log_path.read_text(encoding="utf-8"))
                st.dataframe(
                    pd.DataFrame(fetch_logs),
                    use_container_width=True,
                    hide_index=True,
                )
            except Exception as e:
                st.error(f"ArXiv ログ読み込み失敗: {e}")

# ── サイドバー（4-C: デイリー/ウィークリー対応）─────────────
    with st.sidebar:
        run_h = cfg.RUN_SCHEDULE_HOUR
        run_m = cfg.RUN_SCHEDULE_MINUTE
        wday  = getattr(cfg, "WEEKLY_RUN_WEEKDAY", 0)
        wday_ja = ["月","火","水","木","金","土","日"][wday]
        now   = datetime.now()

        st.markdown("### 🚀 手動実行")

        # 全実行
        if st.button("▶ 全タスク実行（all）", use_container_width=True):
            with st.spinner("全モジュール実行中..."):
                run_all_tasks(mode="all")
            st.success("完了！"); st.rerun()

        # デイリーのみ
        if st.button("☀️ デイリー実行（daily）", use_container_width=True):
            with st.spinner("デイリーモジュール実行中..."):
                run_all_tasks(mode="daily")
            st.success("完了！"); st.rerun()

        # ウィークリーのみ
        if st.button("📅 ウィークリー実行（weekly）", use_container_width=True):
            with st.spinner("ウィークリーモジュール実行中..."):
                run_all_tasks(mode="weekly")
            st.success("完了！"); st.rerun()

        # ArXiv単体
        if st.button("📡 ArXiv のみ実行", use_container_width=True):
            with st.spinner("ArXiv 取得中..."):
                run_arxiv_monitor()
            st.success("完了！"); st.rerun()

# テスト通知ボタン
        if getattr(cfg, "ENABLE_ALERT", False):
            if st.button("🔔 テスト通知を送信", use_container_width=True):
                with st.spinner("通知送信中..."):
                    try:
                        _an = _load_local_module(
                            "alert_notifier",
                            "/content/alert_notifier.py",
                            cfg.DRIVE_BASE / "alert_notifier.py",
                        )
                        if _an:
                            r = _an.send_test_notification(cfg)
                            ok_ch = []
                            if r.get("line"):  ok_ch.append("LINE")
                            if r.get("gmail"): ok_ch.append("Gmail")
                            if ok_ch:
                                st.success(f"送信完了: {', '.join(ok_ch)}")
                            else:
                                errs = r.get("errors", ["チャネル未設定"])
                                st.error("\n".join(errs))
                        else:
                            st.error("alert_notifier.py が見つかりません")
                    except Exception as e:
                        st.error(f"エラー: {e}")

        st.divider()

        # 次回実行予定
        st.markdown("### ⏰ 次回スケジュール")
        next_daily = now.replace(
            hour=run_h, minute=run_m, second=0, microsecond=0
        )
        if next_daily <= now:
            next_daily = next_daily.replace(day=next_daily.day + 1)
        days_to_weekly = (wday - now.weekday()) % 7 or 7
        next_weekly = (now + timedelta(days=days_to_weekly)).replace(
            hour=run_h, minute=run_m, second=0, microsecond=0
        )
        st.caption(f"☀️ デイリー : {next_daily.strftime('%m/%d %H:%M')}")
        st.caption(f"📅 ウィークリー: {next_weekly.strftime('%m/%d(%a) %H:%M')} ({wday_ja}曜)")

        st.divider()

        # 設定状態
        st.markdown("### ⚙️ モジュール状態")
        module_status = [
            ("政府データ",        cfg.ENABLE_GOV_PIPELINE,                      "daily"),
            ("マクロ分析",        cfg.ENABLE_MACRO,                              "daily"),
            ("セクター分析",      cfg.ENABLE_SECTOR,                             "daily"),
            ("タイムライン",      getattr(cfg,"ENABLE_TIMELINE",False),           "daily"),
            ("政策金利",          getattr(cfg,"ENABLE_POLICY_TRACKER",False),     "daily"),
            ("ArXiv",            cfg.ENABLE_ARXIV,                              "weekly"),
            ("相関監査",          getattr(cfg,"ENABLE_CORRELATION",False),        "weekly"),
            ("アイデア生成",      getattr(cfg,"ENABLE_IDEA_GENERATOR",False),     "weekly"),
            ("アイデア評価",      getattr(cfg,"ENABLE_IDEA_EVALUATOR",False),     "weekly"),
            ("アラート通知",      getattr(cfg,"ENABLE_ALERT",False),              "daily"),
        ]
        for label, enabled, cycle in module_status:
            cycle_icon = "☀️" if cycle == "daily" else "📅"
            status     = "✅" if enabled else "❌"
            st.caption(f"{cycle_icon} {status} {label}")

        st.divider()
        st.caption(
            f"設定変更は config.py を編集してください。\n"
            f"スケジュール: 毎日 {run_h:02d}:{run_m:02d} / "
            f"毎週{wday_ja}曜 ウィークリー追加"
        )


# ==============================================================
# BLOCK 8: スケジューラー
# ==============================================================

def start_scheduler():
    """デイリー＋ウィークリーのバックグラウンドスケジューラーを起動する"""
    run_time     = f"{cfg.RUN_SCHEDULE_HOUR:02d}:{cfg.RUN_SCHEDULE_MINUTE:02d}"
    weekday_map  = {0:"monday",1:"tuesday",2:"wednesday",3:"thursday",
                    4:"friday",5:"saturday",6:"sunday"}
    weekly_day   = getattr(cfg, "WEEKLY_RUN_WEEKDAY", 0)
    weekly_label = weekday_map.get(weekly_day, "monday")

    # デイリー: daily モジュールのみ
    schedule.every().day.at(run_time).do(run_all_tasks, mode="daily")
    # ウィークリー: 指定曜日に daily + weekly モジュールを追加実行
    getattr(schedule.every(), weekly_label).at(run_time).do(
        run_all_tasks, mode="weekly"
    )

    print(f"  ⏰ スケジューラー起動:")
    print(f"     デイリー  : 毎日 {run_time} [daily モード]")
    print(f"     ウィークリー: 毎週{weekly_label} {run_time} [weekly モード]")
    print(f"  ⚠️  Colab セッション中のみ有効")
    while True:
        schedule.run_pending()
        time.sleep(60)


# ==============================================================
# エントリーポイント
# ==============================================================

if __name__ == "__main__":
    setup_dirs()

    if "--ui" in sys.argv:
        run_streamlit_ui()
    elif "--schedule" in sys.argv:
        import threading
        t = threading.Thread(target=start_scheduler, daemon=True)
        t.start()
        run_all_tasks()
    else:
        run_all_tasks()
