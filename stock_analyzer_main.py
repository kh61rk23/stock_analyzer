# ==============================================================
# stock_analyzer_main.py — 自律型投資AIエージェント基盤 メイン制御 + Streamlit UI
# ==============================================================
# 設定は import config as cfg で全て参照する。各分析モジュールは _load_local_module で動的ロードする
# （stock_analyzer_main.py 自身は sector_agents/*.py を import しない＝循環参照なし）。
# 主な機能: マクロ指標取得・追い風/逆風判定（fetch_macro_snapshot / evaluate_macro_signals）、
#   33業種エージェント（run_sector_analysis）、Streamlit UI、各種レポートの Markdown 保存。
# ==============================================================

import subprocess, sys, json, time
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

import schedule # type: ignore
import pandas as pd # type: ignore

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
                # 各モジュールの記録ごとに、スクリーナー候補の総合点が動いたかを検出して
                # 「点数の経路」に積む（score_trail.py）
        try:
            import score_trail as _st
            _st.checkpoint(module)
        except Exception:
            pass
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
        import yf_cache as yf
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
            try:
                status = f"{float(price):.3g}" if price else "N/A"
            except (TypeError, ValueError):
                status = str(price)[:15] if price else "N/A"
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
    sector_results の composite_score の順位と momentum を元に
    各業種を TOP / MID / BOT の3段階に分類する。

    スコア順位で上位 SECTOR_TIER_TOP_SHARE を TOP、下位 SECTOR_TIER_BOT_SHARE を
    BOT、残りを MID とする（33業種なら 11 / 11 / 11）。固定閾値だと大半の業種が BOT に
    なり、スクリーナーの対象が数業種に偏る。

    Returns:
        {sector_code: "TOP" / "MID" / "BOT"}
    """
    top_share   = float(getattr(cfg, "SECTOR_TIER_TOP_SHARE", 1 / 3))
    bot_share   = float(getattr(cfg, "SECTOR_TIER_BOT_SHARE", 1 / 3))
    up_slope    = getattr(cfg, "SECTOR_TIER_UPGRADE_SLOPE",   2.5)
    down_slope  = getattr(cfg, "SECTOR_TIER_DOWNGRADE_SLOPE", -2.5)
    crisis_max  = getattr(cfg, "SECTOR_TIER_CRISIS_MAX_TOP",  3)

    # 市場フェーズ取得（inference_engine が未実行の場合は "neutral"）
    market_phase = results.get("economic_cycle", {}).get("phase", "B")
    is_crisis    = results.get("inference", {}).get("phase_code") in ("crisis",)

    # momentumスロープ取得（momentum_factor の結果）
    momentum_res = results.get("momentum", {})

    def _score(item):
        try:
            return float(item[1].get("composite_score", 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    ranked = sorted(
        ((c, r) for c, r in sector_results.items() if isinstance(r, dict)),
        key=_score, reverse=True,
    )
    n_ranked = len(ranked)
    n_top = int(round(n_ranked * top_share))
    n_bot = int(round(n_ranked * bot_share))
    rank_of = {code: i for i, (code, _) in enumerate(ranked)}

    tier = {}
    for code, res in sector_results.items():
        if not isinstance(res, dict):
            tier[code] = "BOT"
            continue

        # ── 基本分類（順位） ──────────────────────────────
        rank = rank_of[code]
        if rank < n_top:
            t = "TOP"
        elif rank >= n_ranked - n_bot:
            t = "BOT"
        else:
            t = "MID"

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
                "weight":         weights.get("oil_price_rise", 1.0),
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
                "weight":         weights.get("us_recession_risk", 1.0),
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
# BLOCK 4: スコア履歴・スクリーナー履歴 蓄積
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
                    # 翌日以降のモメンタム計算で自分の補正を差し引くために残す
                    "momentum_adj": float(r.get("momentum_adjustment", 0.0) or 0.0),
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
    base_agent.run_sector_analysis() を呼ぶ。
    失敗時は config ベースのデフォルト結果を返す。

    Returns:
        dict: cfg.SECTOR_AGENT_RESULT_SCHEMA に準拠した辞書
    """
    sector_info = cfg.JAPAN_SECTORS.get(sector_code, {})

    try:
        import importlib
        base_agent = importlib.import_module("base_agent")
        if hasattr(base_agent, "run_sector_analysis"):
            return base_agent.run_sector_analysis(
                sector_code, cfg, macro_snapshot=macro_snapshot
            )
    except Exception as e:
        print(f"  ⚠️  セクター分析失敗 ({sector_code}): {e}")

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

    repo_root = Path(__file__).resolve().parent
    local = Path(local_path)
    candidates = [local]                         # /content/xxx.py（Colab）
        # GitHub Actions では /content/paper/x.py の階層をリポジトリ直下に写像する
        # （paper/ と ml/ 配下のファイルも見つけるため）。
    try:
        candidates.append(repo_root / local.relative_to("/content"))
    except ValueError:
        pass
    candidates.extend([
        Path(drive_path),                         # Google Drive パス
        repo_root / f"{name}.py",               # リポジトリ直下
    ])

    found = None
    seen = set()
    for p in candidates:
        resolved = str(p.resolve())
        if resolved in seen:
            continue
        seen.add(resolved)
        if p.is_file():
            found = p
            break

    if found is None:
        return None

    try:
        spec = importlib.util.spec_from_file_location(name, str(found))
        if spec is None or spec.loader is None: return None
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception as e:
        print(f"  ❌ {name} ロード失敗: {e}")
        return None

# ==============================================================
# [D-1] 投資判断カード生成（Claude API不使用・ルールベース）
# ==============================================================

def _generate_investment_cards(
    candidates: list,
    results:    dict,
    cfg,
) -> None:
    """
    [D-1] 各 screener 通過銘柄に「なぜBuyか／なぜAvoidか」の
    自然言語サマリーを5行以内で生成し、candidate["investment_card"] に格納する。

    Claude API は使用しない。
    score_explainer / inference_engine / comps の出力を
    ルールベースで統合して生成する。
    """
    import sys as _sys, importlib as _imp
    _sys.path.insert(0, str(Path(__file__).parent))

    # score_explainer をロード
    try:
        _se = _imp.import_module("score_explainer")
    except Exception:
        _se = None

    # comps キャッシュ読み込み
    comps_cache = {}
    try:
        _ca = _imp.import_module("comps_analyzer")
        comps_cache = _ca._load_cache(cfg) or {}
    except Exception:
        pass

    # economic_cycle フェーズ
    ec_phase = results.get("economic_cycle", {}).get("phase", "B")
    phase_labels = {
        "A": "インフレ拡大局面",
        "B": "成長安定局面",
        "C": "スタグフレーション局面",
        "D": "デフレ・縮小局面",
    }
    phase_label = phase_labels.get(ec_phase, "不明局面")

    japan_sectors = getattr(cfg, "JAPAN_SECTORS", {})

    for c in candidates:
        ticker         = c.get("ticker", "")
        comp_score     = c.get("composite_score") or 50.0
        strategy_label = c.get("strategy_label", "")
        sector_code    = c.get("sector_code", "")
        sector_name    = japan_sectors.get(sector_code, {}).get("name_ja", sector_code)
        pass_filters   = c.get("pass_filters", False)
        acc_bonus      = c.get("acc_bonus") or 0.0
        ceo_score      = c.get("ceo_score") or 0.0
        lt_score       = c.get("longterm_scenario_score") or 0.0
        conviction     = c.get("conviction", "low")
        tier           = c.get("tier", "MID")

        reasons_buy   = []
        reasons_avoid = []

        # ① スコア評価
        if comp_score >= 70:
            reasons_buy.append(
                f"総合スコア{comp_score:.0f}pt（上位水準）— "
                f"{sector_name}の{phase_label}における優位性が高い"
            )
        elif comp_score < 50:
            reasons_avoid.append(
                f"総合スコア{comp_score:.0f}pt（低水準）— "
                f"現在の{phase_label}では相対的に不利"
            )

        # ② EPS加速度
        if acc_bonus >= 5:
            reasons_buy.append(
                f"EPS加速スコア+{acc_bonus:.0f}pt — "
                f"直近4四半期で増益加速傾向（I-1検知）"
            )
        elif acc_bonus <= -3:
            reasons_avoid.append(
                f"EPS減速（加速スコア{acc_bonus:.0f}pt）— "
                f"増益率が鈍化傾向"
            )

        # ③ 経営者評価
        if ceo_score >= 70:
            reasons_buy.append(
                f"経営者スコア{ceo_score:.0f}pt — "
                f"中計達成率・資本配分が高評価（I-3）"
            )
        elif ceo_score > 0 and ceo_score < 40:
            reasons_avoid.append(
                f"経営者スコア低（{ceo_score:.0f}pt）— "
                f"過去の公約達成率・ROE改善が不十分"
            )

        # ④ 長期シナリオ
        if lt_score >= 65:
            reasons_buy.append(
                f"長期シナリオスコア{lt_score:.0f}pt — "
                f"10年後の産業地図で{sector_name}は有望（R-5）"
            )
        elif lt_score > 0 and lt_score < 40:
            reasons_avoid.append(
                f"長期シナリオ低（{lt_score:.0f}pt）— "
                f"構造的な逆風（技術変化・地政学）リスク"
            )

        # ⑤ comps（同業種内バリュエーション）
        comps_entry = comps_cache.get(ticker, {})
        if isinstance(comps_entry, dict):
            per_vs_med = comps_entry.get("per_vs_median")
            pbr_vs_med = comps_entry.get("pbr_vs_median")
            if per_vs_med is not None:
                if per_vs_med < 0.8:
                    reasons_buy.append(
                        f"同業種比PER割安（中央値比{per_vs_med:.1%}）— "
                        f"バリュエーション面で割安感あり（5-2）"
                    )
                elif per_vs_med > 1.3:
                    reasons_avoid.append(
                        f"同業種比PER割高（中央値比{per_vs_med:.1%}）— "
                        f"株価に割高感（5-2）"
                    )

        # ⑥ フィルター通過状況
        if not pass_filters:
            reasons_avoid.append(
                "基本フィルター未通過（PER/PBR/時価総額基準外）"
            )

        # ⑦ VaR・相関制約
        if c.get("var_constrained"):
            reasons_avoid.append(
                "VaR制約適用中（ポートフォリオリスク高 / C-1）— "
                "追加購入は慎重に"
            )
        if c.get("corr_excluded"):
            reasons_avoid.append(
                f"相関制約除外（{c.get('corr_cluster','')}内で優先度低 / C-2）"
            )

        # ⑧ 確信度サマリー
        conviction_jp = {"high": "高確信度", "medium": "中確信度", "low": "低確信度"}.get(
            conviction, "確信度不明"
        )

        # ── カード生成 ───────────────────────────────────────
        if strategy_label in ("core", "satellite_event", "satellite_smallcap"):
            # Buy系カード
            summary_lines = [f"✅ {conviction_jp} / {strategy_label}"] + reasons_buy[:4]
            verdict = "BUY"
        else:
            # Avoid系カード
            summary_lines = ["⚠️ 現時点では見送り"] + reasons_avoid[:4]
            verdict = "AVOID" if not pass_filters else "WATCH"

        # score_explainer による1行説明を付加
        if _se:
            try:
                macro_snap  = results.get("macro_snapshot", {})
                sec_result  = results.get("sectors", {}).get(sector_code, {})
                explanation = _se.explain_score(
                    sector_code    = sector_code,
                    composite_score= comp_score,
                    sector_result  = sec_result,
                    macro_snapshot = macro_snap,
                )
                if explanation:
                    summary_lines.append(f"📊 スコア根拠: {explanation}")
            except Exception:
                pass

        c["investment_card"] = {
            "verdict":      verdict,
            "summary":      "\n".join(summary_lines[:5]),
            "reasons_buy":  reasons_buy[:4],
            "reasons_avoid": reasons_avoid[:4],
            "conviction":   conviction,
            "phase":        phase_label,
            "generated_at": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M"),
        }

    bought_count = sum(1 for c in candidates if c.get("investment_card", {}).get("verdict") == "BUY")
    print(f"    🃏 [D-1] 投資判断カード生成: {len(candidates)}銘柄 "
          f"（BUY:{bought_count} / AVOID/WATCH:{len(candidates)-bought_count}）")
    
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
        # composite_score の上下限は config（SCORE_MIN_COMPOSITE / SCORE_MAX_COMPOSITE）から読む。
    score_min = float(getattr(cfg, "SCORE_MIN_COMPOSITE", 0.0))
    score_max = float(getattr(cfg, "SCORE_MAX_COMPOSITE", 100.0))

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

        new_score = round(
            max(score_min, min(score_max, old_score + capped_delta)), 1)
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
        return {}   # 常に dict（下流の B-2 等が results["gov"].get() する）

    try:
        result = mod.main(cfg=cfg)
        fetched = result.get("stats_fetched", 0) if isinstance(result, dict) else 0
        total   = result.get("stats_total",   0) if isinstance(result, dict) else 0
        logger.add("gov_pipeline", "success", f"政府統計取得完了: {fetched}/{total}")
        return result if isinstance(result, dict) else {}
    except Exception as e:
        msg = str(e)[:120]
        print(f"  ❌ 実行エラー: {msg}")
        logger.add("gov_pipeline", "error", msg)
        return {}   # 常に dict（arxiv と同じ契約）


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
        return {}   # 常に dict（etf_tracker が dict チェックする）

    try:
        arxiv_result = mod.run_monitor()

        # 戻り値が dict でない場合（旧バージョン互換）は空 dict に変換
        if not isinstance(arxiv_result, dict):
            arxiv_result = {}

        n        = arxiv_result.get("investment_ideas_found", 0)
        n_papers = arxiv_result.get("new_papers", 0)
        bq_err   = (arxiv_result.get("bq") or {}).get("error")
        if bq_err:
            # 取れたのに BQ に書けなかった回は失敗として残す（既読にもしていないので次回再保存される）
            logger.add("arxiv_monitor", "error", f"新着{n_papers}件, 候補{n}件 / BQ保存失敗: {bq_err}")
        else:
            logger.add("arxiv_monitor", "success",
                       f"新着{n_papers}件, 候補{n}件 / BQ {(arxiv_result.get('bq') or {}).get('rows', 0)}行")
        return arxiv_result

    except Exception as e:
        msg = str(e)[:120]
        print(f"  ❌ ArXiv 監視エラー: {msg}")
        logger.add("arxiv_monitor", "error", msg)
        return {}   # 常に dict


def _save_macro_report(snapshot: dict, signals: list):
    """マクロ分析レポートを Markdown で保存する（新規追加）"""
    cfg.MACRO_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    path  = cfg.MACRO_DIR / f"macro_report_{today}.md"

    md  = "# マクロ経済 分析レポート\n\n"
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
        # delta系キー（_delta, _alert等）と非数値はスキップ
        if k.startswith("_") or k.endswith("_delta") or k.endswith("_alert"):
            continue
        label = labels.get(k, k)
        if label == k and k not in labels:
            continue   # labelsに定義されていないキーはスキップ
        try:
            # report_store.py がこの表を読んで BQ の macro_indicators_daily に入れるので、丸めすぎない（有効3桁だと日経が 6.91e+04 になる）
            val = f"{float(v):,.2f}" if v is not None else "N/A"
        except (TypeError, ValueError):
            val = str(v)[:20] if v is not None else "N/A"
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

from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'SAM_DELTA_THRESHOLDS': '_DELTA_THRESHOLDS',
}

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
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
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

    # 下流モジュールが参照するため ENABLE_SECTOR/ENABLE_MACRO=False 時も定義しておく
    macro_snapshot_for_sector = {}
    sector_results            = {}

    results = {
        "sectors":      {},
        "screener":     {},
        "geo_risk":     {},   # G-N: 国別リスク日次更新結果
              }
    source_ingest_done = {"edinet": False, "fudosan": False, "us": False}
    try:
        import score_trail as _st_watch
        _st_watch.watch(results)
    except Exception:
        pass
        # ── [Policy Ph2] EMERGENCY_OVERRIDE_FLAG チェック ──
        # VIX > EMERGENCY_VIX_THRESHOLD(45.0) の場合、政策シグナルを全停止
    try:
        from policy_signal_applier import get_emergency_override_flag as _get_em
        _emergency_active = _get_em(results, cfg)
        results["emergency_override"] = _emergency_active
        if _emergency_active:
            print("  🚨 EMERGENCY_OVERRIDE_FLAG: ON（VIX超過検知）"
                  " - 政策シグナルは今回停止")
    except Exception:
        results["emergency_override"] = False

    # ── [Issue J-2] APIキー生存判定（最序盤・マクロ層の前）─────────
    # 各APIキーの生存/失効を実行ログ冒頭で 1行サマリ＋詳細表として一望する。
    if getattr(cfg, "ENABLE_KEY_HEALTH", True) and _in_mode("key_health"):
        try:
            _kh = _load_local_module(
                "api_key_health",
                "/content/api_key_health.py",
                cfg.DRIVE_BASE / "api_key_health.py",
            )
            if _kh:
                kh_result = _kh.run_key_health(cfg)
                results["api_key_health"] = kh_result
                logger.add(
                    "api_key_health",
                    kh_result.get("ledger_status", "success"),
                    kh_result.get("summary", ""),
                )
        except Exception as _khe:
            print(f"  ⚠️  APIキー生存判定エラー（スキップ）: {_khe}")

    # ── EdinetDB・政府 EDINET・不動産DB の取り込み（fetch_stages.jp_filings）──────
    # 取得 workflow（fetch_jp_filings.yml）が朝に済ませていれば、その結果を写すだけにする。
    # 無ければ（workflow が落ちた・まだ終わっていない・Mac で動かした等）この回で取る。
    try:
        import fetch_stages as _fs
        if not _fs.apply_external(cfg, "jp_filings", results, logger):
            _fs.run_jp_filings(cfg, results, logger, _in_mode)
        source_ingest_done["edinet"] = "edinetdb_ingest" in results
        source_ingest_done["fudosan"] = "fudosan_ingest" in results
    except Exception as exc:
        logger.add("edinetdb_ingest", "error", f"fetch_stages: {str(exc)[:100]}")

    if getattr(cfg, "ENABLE_US_FUNDAMENTALS", True) and _in_mode("us_fundamentals"):
        try:
            _ufr = _load_local_module("us_fundamental_rotator", "/content/us_fundamental_rotator.py", cfg.DRIVE_BASE / "us_fundamental_rotator.py")
            if _ufr:
                results["us_fundamentals"] = _ufr.run_us_fundamental_rotator(cfg, results)
                source_ingest_done["us"] = True
                logger.add("us_fundamentals", "success", str(results["us_fundamentals"]))
        except Exception as exc:
            logger.add("us_fundamentals", "error", str(exc)[:100])

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

        # ── [B-2] GOV_RULES による先行指標シグナル生成 ──────────────
        if getattr(cfg, "ENABLE_GOV_RULES", True):
            try:
                _gov_rules   = getattr(cfg, "GOV_RULES", {})
                _gov_signals = []
                                # `or {}`: gov pipeline が失敗時に非dict（False 等）を返しても落ちないようにする。
                _gov_result  = results.get("gov", {}) or {}
                # gov_data_pipeline は stats/{id} の形式で値を返す
                _gov_stats   = _gov_result.get("stats", {}) if isinstance(_gov_result, dict) else {}

                for rule_id, rule in _gov_rules.items():
                    trigger    = rule.get("trigger", {})
                    key        = trigger.get("key", "")
                    op         = trigger.get("op", ">=")
                    val        = trigger.get("val", 0.0)
                    pts        = rule.get("pts_adj", 3.0)

                    stat_entry = _gov_stats.get(key) or {}
                    metric_val = None
                    if isinstance(stat_entry, dict):
                        metric_val = (
                            stat_entry.get("yoy") or
                            stat_entry.get("change_pct") or
                            stat_entry.get("latest_value")
                        )
                    elif isinstance(stat_entry, (int, float)):
                        metric_val = float(stat_entry)

                    if metric_val is None:
                        continue

                    triggered = (
                        (op == ">=" and metric_val >= val) or
                        (op == ">"  and metric_val >  val) or
                        (op == "<=" and metric_val <= val) or
                        (op == "<"  and metric_val <  val)
                    )
                    if not triggered:
                        continue

                    sig = rule.get("signal", {})
                    for code in sig.get("tailwind", []):
                        _gov_signals.append({
                            "rule_id":     f"GOV_{rule_id}",
                            "label":       rule.get("label", rule_id),
                            "sector_code": code,
                            "signal":      "tailwind",
                            "pts_adj":     abs(pts),
                            "source":      "gov_rules",
                            "metric_val":  round(float(metric_val), 3),
                        })
                    for code in sig.get("headwind", []):
                        _gov_signals.append({
                            "rule_id":     f"GOV_{rule_id}",
                            "label":       rule.get("label", rule_id),
                            "sector_code": code,
                            "signal":      "headwind",
                            "pts_adj":     -abs(pts),
                            "source":      "gov_rules",
                            "metric_val":  round(float(metric_val), 3),
                        })

                if _gov_signals:
                    _existing = results.get("leading_indicator_signals", [])
                    results["leading_indicator_signals"] = _existing + _gov_signals
                    print(f"    📊 [B-2] GOV_RULES: {len(_gov_signals)}件発動")

            except Exception as _b2e:
                print(f"  ⚠️  [B-2] GOV_RULES評価エラー: {_b2e}")
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

# ── [G-N] 国別リスク日次更新 ──────────────────────────
    # country_risk_updater: GDELT×外務省RSS×国務省RSS を集約して
    # geopolitics.country_risk_daily を日次 MERGE する。
    # ネットワーク障害・BQ接続失敗時も全停止させず 0.0 スコアで後続処理を継続する
    if getattr(cfg, "ENABLE_COUNTRY_RISK_UPDATE", False) and _in_mode("macro"):
        try:
            _cru = _load_local_module(
                "country_risk_updater",
                "/content/country_risk_updater.py",
                cfg.DRIVE_BASE / "country_risk_updater.py",
            )
            if _cru:
                print("\n  🌐 国別リスク更新開始...")
                _geo_risk_result = _cru.run_country_risk_update(
                    cfg        = cfg,
                    project_id = getattr(cfg, "GEO_BQ_PROJECT_ID",
                                         "stock-analyzer-edinet"),
                    skip_gdelt = getattr(cfg, "GEO_SKIP_GDELT", False),
                    dry_run    = False,
                )
                results["geo_risk"] = _geo_risk_result

                # 高リスク国を results に記録（weekly_report_generator が参照）
                _high_risk = _geo_risk_result.get("high_risk", [])
                if _high_risk:
                    print(
                        f"    🔴 [G-N] 高リスク国検出: "
                        f"{_high_risk[:5]}"
                    )
                logger.add(
                    "geo_risk",
                    "success" if _geo_risk_result.get("success") else "error",
                    f"国別リスク更新: "
                    f"{_geo_risk_result.get('updated', 0)}件更新 "
                    f"高リスク{len(_high_risk)}カ国",
                )
            else:
                print("  ⚠️  country_risk_updater.py が見つかりません（スキップ）")
                results["geo_risk"] = {"skipped": True}
        except Exception as _gne:
            print(f"  ⚠️  [G-N] 国別リスク更新エラー（スキップ）: "
                  f"{str(_gne)[:80]}")
            results["geo_risk"] = {"skipped": True, "error": str(_gne)[:200]}
    else:
        if not getattr(cfg, "ENABLE_COUNTRY_RISK_UPDATE", False):
            print("  ⏩ 国別リスク更新: スキップ"
                  "（ENABLE_COUNTRY_RISK_UPDATE=False）")
        results.setdefault("geo_risk", {"skipped": True})

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
                # 実行台帳へ記録する（DAILY_MODULES の mode-key "macro" と一致させる）。
        logger.add(
            "macro", "success",
            f"マクロ{len(signals)}シグナル / 急変{len(results['macro_delta_alerts'])}件"
        )
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
    sector_results = {}   # 常に初期化（_macro_weights の有無に関係なく使う）
    if _macro_weights:
        # 重みを snapshot に埋め込んで sector_agents に伝達
        macro_snapshot_for_sector["_weights"] = _macro_weights
        # ── 業種分析の取得部分だけを並列化 ──────────────
        # 1業種あたり約20〜24秒の大半が BQ・yfinance・EDINET の待ち時間なので並列化が効く。
        # 後続の補正（債務サイクル/経済サイクル/横断シグナル等）は results への参照順に依存するので逐次。
        # SECTOR_ANALYSIS_WORKERS=1 で逐次実行。
    _sec_workers = max(1, int(getattr(cfg, "SECTOR_ANALYSIS_WORKERS", 4)))
    _sec_raw: dict = {}
    if _sec_workers > 1 and len(cfg.IMPLEMENTED_AGENTS) > 1:
        from concurrent.futures import ThreadPoolExecutor

        def _run_one_sector(_code):
            try:
                return _code, run_sector_analysis(
                    _code, macro_snapshot_for_sector)
            except Exception as _e:      # 1業種の失敗を全体に波及させない
                return _code, _e

        print(f"  ⚡ 業種分析を{_sec_workers}並列で実行"
              f"（{len(cfg.IMPLEMENTED_AGENTS)}業種・出力順は前後します）")
        with ThreadPoolExecutor(max_workers=_sec_workers) as _pool:
            for _c, _r in _pool.map(_run_one_sector, cfg.IMPLEMENTED_AGENTS):
                _sec_raw[_c] = _r
    else:
        for _c in cfg.IMPLEMENTED_AGENTS:
            try:
                _sec_raw[_c] = run_sector_analysis(
                    _c, macro_snapshot_for_sector)
            except Exception as _e:
                _sec_raw[_c] = _e

    for code in cfg.IMPLEMENTED_AGENTS:
        try:
            res = _sec_raw.get(code)
            if isinstance(res, BaseException):
                raise res
            sector_results[code] = res
            # ── [D-2] 債務サイクル一律補正 ──────────────────
            _dc_adj = macro_snapshot_for_sector.get("_debt_cycle_adj", 0.0)
            _dc_actual = 0.0
            if _dc_adj != 0.0 and isinstance(res, dict):
                _dc_cap = getattr(cfg, "SCORE_MAX_CYCLE_ADJ", 10.0)
                _dc_actual = max(-_dc_cap, min(_dc_cap, _dc_adj))
                
            res["composite_score"] = round(
                max(-30, min(100, res.get("composite_score", 0) + _dc_actual)), 1
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
                    _actual = max(-_cap, min(_cap, _delta))
                    res["composite_score"] = round(
                        max(-30, min(100, _old_s + _actual)), 1
                    )
                    res["cycle_phase_adjustment"] = _actual

                    # ── [R-1] 横断産業シグナル補正 ─────────────────
                    _cs_signals = results.get("cross_sector_signals", [])
                                        # 集計と判定はループ内で1シグナルずつ行う（ループ外に出すと、シグナルが空のとき
                                        #   _tw が未定義になり、値があっても最後の1件しか評価されない）。
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

                        # ── [S3-2] 動的アロケーション切り替え ──────────
                        # sector_tier（TOP/MID/BOT）に基づき、
                        # 各業種の取得予算（edinet_rotation のバジェット）を動的に調整する。
                        # TOP業種: 取得予算を 1.5倍 / BOT業種: 0.5倍 / MID: 1.0倍（変更なし）
                        # ENABLE_S3_DYNAMIC_ALLOC=True かつ sector_tier が存在する場合のみ実行。
            if getattr(cfg, "ENABLE_S3_DYNAMIC_ALLOC", False):
                try:
                    _alloc_map: dict = {}
                    _base_budget = int(getattr(cfg, "EDINET_DAILY_BUDGET", 10))

                    for _code, _t in _tier.items():
                        if _t == "TOP":
                            _alloc_map[_code] = round(_base_budget * 1.5)
                        elif _t == "BOT":
                            _alloc_map[_code] = round(_base_budget * 0.5)
                        else:
                            _alloc_map[_code] = _base_budget

                    # cfg に動的アロケーションマップを注入
                    # edinet_rotation_manager._build_fetch_queue() が
                    # cfg.EDINET_SECTOR_BUDGET_MAP を参照して予算を調整する
                    cfg.__dict__["EDINET_SECTOR_BUDGET_MAP"] = _alloc_map
                    results["sector_alloc"] = _alloc_map

                    _top_alloc = {c: b for c, b in _alloc_map.items()
                                  if _tier.get(c) == "TOP"}
                    _bot_alloc = {c: b for c, b in _alloc_map.items()
                                  if _tier.get(c) == "BOT"}
                    print(
                        f"    💰 [S3-2] 動的アロケーション: "
                        f"TOP×1.5→{list(_top_alloc.keys())[:3]} / "
                        f"BOT×0.5→{list(_bot_alloc.keys())[:3]}"
                    )
                except Exception as _s32e:
                    print(f"    ⚠️  [S3-2] 動的アロケーションエラー（スキップ）: "
                          f"{str(_s32e)[:60]}")

        except Exception as _tier_e:
            print(f"  ⚠️  sector_tier 計算エラー: {_tier_e}")
        
                # ── [S3-3] confidence_prior 重み付き更新 ───────────
                # sector_tier（TOP/MID/BOT）に基づき、
                # composite_score の confidence_prior を EMA で更新する。
                # $new\_prior = old\_prior \times (1 - \alpha) + score \times \alpha$
                # $\alpha = CONFIDENCE\_PRIOR\_ALPHA (0.15)$
                # TOP業種はαを1.5倍（より積極的に更新）、BOT業種は0.5倍（保守的）。
                # ENABLE_S3_CONFIDENCE_PRIOR=True の場合のみ実行。
        if getattr(cfg, "ENABLE_S3_CONFIDENCE_PRIOR", False):
            try:
                _alpha_base = float(getattr(cfg, "CONFIDENCE_PRIOR_ALPHA", 0.15))
                _tier_map   = results.get("sector_tier", {})
                _conf_prior = results.get("confidence_prior", {})
                                # results は毎回空から始まるため、前回の prior は score_history から読む
                if not _conf_prior:
                    try:
                        import json as _json_p
                        _shp = Path(cfg.DATA_DIR) / "score_history.json"
                        _shd = _json_p.loads(_shp.read_text(encoding="utf-8")) if _shp.exists() else {}
                        _today_p = datetime.now().strftime("%Y-%m-%d")
                        for _d in sorted((k for k in _shd if k < _today_p), reverse=True):
                            _cp = {c: v["confidence_prior"] for c, v in (_shd.get(_d) or {}).items()
                                   if isinstance(v, dict) and v.get("confidence_prior") is not None}
                            if _cp:
                                _conf_prior = _cp
                                break
                    except Exception as _cpe:
                        print(f"    ⚠️  [S3-3] 前回 prior の読み込み失敗: {str(_cpe)[:60]}")

                _updated_prior: dict = {}
                for _code, _sres in sector_results.items():
                    if not isinstance(_sres, dict):
                        continue
                    _score = _sres.get("composite_score")
                    if _score is None:
                        continue
                    _score = float(_score)

                    # tier に応じて alpha を調整
                    _t = _tier_map.get(_code, "MID")
                    if _t == "TOP":
                        _alpha = min(0.40, _alpha_base * 1.5)
                    elif _t == "BOT":
                        _alpha = max(0.05, _alpha_base * 0.5)
                    else:
                        _alpha = _alpha_base

                    # EMA 更新
                    _old_prior = float(_conf_prior.get(_code, _score))
                    _new_prior = round(
                        _old_prior * (1 - _alpha) + _score * _alpha, 2
                    )
                    _updated_prior[_code] = _new_prior

                    # sector_results に prior を埋め込む
                    if isinstance(sector_results.get(_code), dict):
                        sector_results[_code]["confidence_prior"] = _new_prior
                        sector_results[_code]["confidence_prior_alpha"] = _alpha

                # 更新した prior を results に保存
                results["confidence_prior"] = _updated_prior
                results["sectors"]          = sector_results

                _prior_changed = sum(
                    1 for c, p in _updated_prior.items()
                    if abs(p - float(_conf_prior.get(c, p))) >= 0.5
                )
                print(
                    f"    📐 [S3-3] confidence_prior 更新: "
                    f"{len(_updated_prior)}業種 / "
                    f"変化{_prior_changed}業種"
                )

                # confidence_prior を score_history に永続保存
                # （次回 score_prior_manager が参照する）
                try:
                    _sh_path_w = Path(cfg.DATA_DIR) / "score_history.json"
                    if _sh_path_w.exists():
                        import json as _json_w
                        _sh_data = _json_w.loads(
                            _sh_path_w.read_text(encoding="utf-8")
                        )
                        _today_str = datetime.now().strftime("%Y-%m-%d")
                        _sh_data.setdefault(_today_str, {})
                        for _code, _prior in _updated_prior.items():
                            if isinstance(_sh_data[_today_str].get(_code), dict):
                                _sh_data[_today_str][_code]["confidence_prior"] = _prior
                            else:
                                _sh_data[_today_str].setdefault(
                                    _code, {}
                                )["confidence_prior"] = _prior
                        _sh_path_w.write_text(
                            _json_w.dumps(_sh_data, ensure_ascii=False),
                            encoding="utf-8",
                        )
                except Exception as _sh_e:
                    pass   # score_history 書き込み失敗はスキップ

            except Exception as _s33e:
                print(
                    f"  ⚠️  [S3-3] confidence_prior 更新エラー（スキップ）: "
                    f"{str(_s33e)[:60]}"
                )

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
                    print("    ✅ [GAP-1] ベイズ更新完了（ドリフトなし）")

                logger.add("bayesian_update", "success",
                           f"更新{len(_updated)}業種 / ドリフト{len(_drifts)}件")
                
                                # ── [S4/F] 鮮度係数による score_prior 重み付け ──
                                # score_prior_manager.apply_freshness_weight_batch() を使い、
                                # 過去スコア履歴の信頼度（prior_weight）で composite_score を補正する。
                                # prior_weight=0.0（データ不足）の業種はそのまま通過する。
                if getattr(cfg, "ENABLE_SCORE_PRIOR", True):
                    try:
                        _spm_mod = _load_local_module(
                            "score_prior_manager",
                            "/content/score_prior_manager.py",
                            cfg.DRIVE_BASE / "score_prior_manager.py",
                        )
                        if _spm_mod:
                            _alpha = float(getattr(
                                cfg, "CONFIDENCE_PRIOR_ALPHA", 0.15
                            ))
                            _updated = _spm_mod.apply_freshness_weight_batch(
                                sector_results = _updated,
                                cfg            = cfg,
                                alpha          = _alpha,
                            )
                            results["sectors"] = _updated

                            # drift_detector を続けて実行
                            _ddt_mod = _load_local_module(
                                "drift_detector",
                                "/content/drift_detector.py",
                                cfg.DRIVE_BASE / "drift_detector.py",
                            )
                            if _ddt_mod:
                                _drift_report = _ddt_mod.run_drift_detector(
                                    cfg, _updated, _sh_all
                                )
                                results["drift_report"] = _drift_report
                                if _drift_report.get("total", 0) > 0:
                                    logger.add(
                                        "drift_detector", "success",
                                        f"急変{len(_drift_report['spike_drifts'])}件 / "
                                        f"劣化{len(_drift_report['decay_drifts'])}件",
                                    )
                    except Exception as _spm_e:
                        print(
                            f"  ⚠️  [Phase 0] score_prior 適用エラー（スキップ）: "
                            f"{str(_spm_e)[:60]}"
                        )

                                # ── [GAP-3] 乖離ログを永続記録（ENABLE_PERFORMANCE_TRACKER）──────────────
                try:
                    if getattr(cfg, "ENABLE_PERFORMANCE_TRACKER", True):
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

                # 規模分類マスターは両スクリーナーの結果が要るので、us_stock_screener の直後で生成する（ここではない）。

                # ── [DAILY] 規模別減衰管理 ──────────────────────────
    if getattr(cfg, "ENABLE_SIZE_DECAY_MANAGER", False) and _in_mode("size_tier"):
        try:
            _sdm = _load_local_module(
                "size_decay_manager",
                "/content/size_decay_manager.py",
                cfg.DRIVE_BASE / "size_decay_manager.py",
            )
            if _sdm:
                sdm_result = _sdm.run_size_decay_manager(cfg, force_refresh=False)
                results["size_decay"] = sdm_result
                _active = sdm_result.get("stats", {}).get("active_tickers", 0)
                _nano   = sdm_result.get("stats", {}).get("nano_pending", 0)
                logger.add(
                    "size_decay_manager", "success",
                    f"アクティブ={_active}銘柄 Nano待機={_nano}",
                )
        except Exception as e:
            logger.add("size_decay_manager", "error", str(e)[:100])
            print(f"  ⚠️  規模別減衰管理エラー: {str(e)[:80]}")

        # ── [DAILY] 規模帯横断シグナル ─────────────────────
    if getattr(cfg, "ENABLE_CROSS_SIZE_SIGNAL", False) and _in_mode("size_tier"):
        try:
            _css = _load_local_module(
                "cross_size_signal",
                "/content/cross_size_signal.py",
                cfg.DRIVE_BASE / "cross_size_signal.py",
            )
            if _css:
                css_result = _css.run_cross_size_signal(cfg, force_refresh=False)
                results["cross_size_signal"] = css_result
                _n_sigs = css_result.get("stats", {}).get("total_signals", 0)
                _n_tick = css_result.get("stats", {}).get("total_tickers", 0)
                logger.add(
                    "cross_size_signal", "success",
                    f"シグナル={_n_sigs}件 関連銘柄={_n_tick}",
                )
        except Exception as e:
            logger.add("cross_size_signal", "error", str(e)[:100])
            print(f"  ⚠️  規模帯横断シグナルエラー: {str(e)[:80]}")

        # ── [DAILY] 市場独立性スコア ───────────────────────
    if getattr(cfg, "ENABLE_INDEPENDENCE_SCORE", False) and _in_mode("screener"):
        try:
            _isc = _load_local_module(
                "independence_score_calculator",
                "/content/independence_score_calculator.py",
                cfg.DRIVE_BASE / "independence_score_calculator.py",
            )
            if _isc:
                max_tk = int(getattr(cfg, "INDEPENDENCE_MAX_TICKERS_PER_RUN", 200))
                isc_result = _isc.run_independence_score_calculator(
                    cfg, force_refresh=False, max_tickers=max_tk
                )
                results["independence_scores"] = isc_result
                _high = isc_result.get("stats", {}).get("high_independence_count", 0)
                logger.add(
                    "independence_score", "success",
                    f"HIGH独立={_high}銘柄 "
                    f"avg|β|={isc_result.get('stats', {}).get('avg_beta_abs', 0):.3f}",
                )
        except Exception as e:
            logger.add("independence_score", "error", str(e)[:100])
            print(f"  ⚠️  独立性スコアエラー: {str(e)[:80]}")

        # ── [DAILY] 潜伏銘柄管理 ──────────────────────────
    if getattr(cfg, "ENABLE_LATENT_PEARL_MANAGER", False) and _in_mode("screener"):
        try:
            _lpm = _load_local_module(
                "latent_pearl_manager",
                "/content/latent_pearl_manager.py",
                cfg.DRIVE_BASE / "latent_pearl_manager.py",
            )
            if _lpm:
                lpm_result = _lpm.run_latent_pearl_manager(cfg, force_refresh=False)
                results["latent_pearl"]         = lpm_result
                results.setdefault("wave7_trackers", {})["latent_pearl"] = lpm_result
                _n_latent   = lpm_result.get("stats", {}).get("total_latent", 0)
                _n_promoted = lpm_result.get("stats", {}).get("promoted_count", 0)
                logger.add(
                    "latent_pearl_manager", "success",
                    f"潜伏={_n_latent}銘柄 昇格済={_n_promoted}",
                )
        except Exception as e:
            logger.add("latent_pearl_manager", "error", str(e)[:100])
            print(f"  ⚠️  潜伏銘柄管理エラー: {str(e)[:80]}")

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

                                # ── bridge 経由の独立性・減衰スコアを results に昇格 ──
                if bridge_result.get("independence_bridge"):
                    results.setdefault("wave7_trackers", {})
                    results["wave7_trackers"]["independence_bridge"] = (
                        bridge_result["independence_bridge"]
                    )
                if bridge_result.get("size_decay_bridge"):
                    results["wave7_trackers"]["size_decay_bridge"] = (
                        bridge_result["size_decay_bridge"]
                    )

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
                                # 実行できた事実は件数に関わらず必ず台帳に記録する（0件の日も「記録なし」にしない）。
                logger.add(
                    "us_sector_bridge", "success",
                    f"JP影響業種{len(usbe_result.get('jp_affected',[]))}業種 / "
                    f"beat{len(usbe_result.get('beat_tickers',[]))} / "
                    f"miss{len(usbe_result.get('miss_tickers',[]))} / "
                    f"cross{len(usbe_result.get('cross_signals',[]))}",
                )
            else:
                print("  ⚠️  us_sector_bridge_enhanced.py が見つかりません")
                logger.add("us_sector_bridge", "warn",
                           "us_sector_bridge_enhanced.py not found")
        except Exception as e:
            logger.add("us_sector_bridge", "error", str(e)[:100])
            print(f"  ⚠️  US-JP セクターブリッジエラー: {str(e)[:80]}")
    else:
        print("  ⏩ US-JP セクターブリッジ: スキップ")
        logger.add("us_sector_bridge", "skipped",
                   "ENABLE_US_SECTOR_BRIDGE=False または対象モード外")
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
                # （_apply_capped_adjustment が各業種へ breadth_adjustment を
                #   設定済み。旧コードにあった
                #   sector_results[_code]["breadth_adjustment"] = _delta は
                #   別ループの stale な _code/_delta を1業種へ誤書きする冗長行
                #   だったため削除。）
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

                                # composite_score に季節補正を適用（anomaly_season 自身の adjustments を使う）
                _season_adj = as_result.get("adjustments", {})
                _apply_capped_adjustment(
                    sector_results,
                    {k: v for k, v in _season_adj.items() if v != 0},
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

                                # composite_score にオプション補正を適用（options_monitor 自身の adjustments を使う）
                _options_adj = om_result.get("adjustments", {})
                _apply_capped_adjustment(
                    sector_results,
                    {k: v for k, v in _options_adj.items() if v != 0},
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
                                        # 市場イベントの検知は正常動作なので warn にしない（台帳の「要確認」に本当の不具合が埋もれる）
                    for ev in pt_result["new_events"]:
                        logger.add("policy_event", "success",
                                   f"検知 [{ev['bank']}] {ev['label']} "
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
                                # 多通貨補正を sector_results に反映（mc_adj＝多通貨自身の補正）
                mc_adj = mc_result.get("macro_adjustment", {})
                _sr    = results.get("sectors", {})
                _apply_capped_adjustment(
                    sector_results,
                    {k: v for k, v in mc_adj.items() if v != 0},
                    adj_key        = "currency_adjustment",
                    cap_config_key = "SCORE_MAX_CROSS_ADJ",
                    label          = "MULTI_CURRENCY",
                )
                # （同上: _apply_capped_adjustment が currency_adjustment を設定済み。
                #   _sr[_code]["currency_adjustment"] = _delta は stale 変数による
                #   誤書き冗長行のため削除。）
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

        # ── [I-2] NEDO採択情報 → edinet.policy_support（週次）────
        # 書き手。読む側は base_agent.get_policy_support_bonus()。
    if getattr(cfg, "ENABLE_NEDO_SCRAPER", True) and _in_mode("nedo"):
        try:
            _nedo = _load_local_module(
                "nedo_selection_scraper",
                "/content/nedo_selection_scraper.py",
                cfg.DRIVE_BASE / "nedo_selection_scraper.py",
            )
            if _nedo:
                # BQ未接続環境では bq_client=None（スクレイプのみ）で続行する
                try:
                    from edinet_bq_client import EDINETBQClient as _NedoBQ
                    _nedo_bq = _NedoBQ()
                except Exception:
                    _nedo_bq = None
                _nedo_res = _nedo.run_nedo_selection_scraper(
                    cfg, results, bq_client=_nedo_bq,
                )
                results["nedo_selection"] = _nedo_res
                logger.add(
                    "nedo_selection_scraper", "success",
                    f"採択{_nedo_res.get('scraped_count', 0)}件 / "
                    f"名寄せ{_nedo_res.get('matched_count', 0)}件 / "
                    f"BQ={_nedo_res.get('bq_inserted', False)}",
                )
            else:
                logger.add("nedo_selection_scraper", "warn",
                           "nedo_selection_scraper.py が見つかりません")
        except Exception as _nde:
            logger.add("nedo_selection_scraper", "error", str(_nde)[:100])
            print(f"  ⚠️  NEDO採択情報エラー（スキップ）: {str(_nde)[:60]}")
    else:
        print("  ⏩ NEDO採択情報: スキップ（週次以外）")

                # ── [FE-7] Catalyst Calendar（WEEKLY）────────────────
    if _in_mode("catalyst_calendar"):
        try:
            _cat = _load_local_module(
                "catalyst_calendar",
                "/content/catalyst_calendar.py",
                cfg.DRIVE_BASE / "catalyst_calendar.py",
            )
            if _cat:
                cat_result = _cat.run_catalyst_calendar(cfg, results)
                results["catalyst"] = cat_result
                logger.add(
                    "catalyst_calendar", "success",
                    cat_result.get("summary", ""),
                )
                if cat_result.get("triple_tickers"):
                    print(
                        f"  ⭐ Triple Catalyst: "
                        f"{len(cat_result['triple_tickers'])}銘柄"
                    )
        except Exception as _cate:
            logger.add("catalyst_calendar", "error", str(_cate)[:100])
            print(f"  ⚠️  Catalyst Calendarエラー（スキップ）: {str(_cate)[:60]}")

        # ── [Step B] ニュースRSSパイプライン ─────────────────────────
        # results["news"] を FinBERT / alert_notifier / emerging_technology_detector が読むので、
        # FinBERT より前に実行する。
    if getattr(cfg, "ENABLE_NEWS", True) and _in_mode("news"):
        try:
            _np = _load_local_module(
                "news_pipeline",
                "/content/news_pipeline.py",
                cfg.DRIVE_BASE / "news_pipeline.py",
            )
            if _np:
                news_result = _np.run_news_pipeline(cfg)
                results["news"] = news_result or {}
                logger.add(
                    "news", "success",
                    f"取得{(news_result or {}).get('fetch_count', 0)}件 "
                    f"| 業種マッチ{(news_result or {}).get('match_count', 0)}件",
                )
        except Exception as _npe:
            results["news"] = {}
            logger.add("news", "error", str(_npe)[:100])
            print(f"  ⚠️  ニュースRSSエラー（スキップ）: {str(_npe)[:60]}")
    else:
        print("  ⏩ ニュースRSS: スキップ")

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

                                # ── [US-2] composite_us_macro_score を業種スコアへ反映 ──
                                # 反映式: 50点を中立とし、乖離を上限内に線形マップする。
                                #   adj = (score - 50) / 50 * US_MACRO_MAX_SECTOR_ADJ
                                #   例) score=100 → +5.0pt / score=0 → -5.0pt / score=50 → 0
                                # 米国マクロは全業種に一様に効くため、業種別の差は付けない。
                                # 上限適用・全補正累積の管理は既存の _apply_capped_adjustment に委ねる。
                try:
                    _um_cap = float(getattr(cfg, "US_MACRO_MAX_SECTOR_ADJ", 5.0))
                    _um_adj = round((float(score) - 50.0) / 50.0 * _um_cap, 2)
                    if sector_results and abs(_um_adj) >= 0.1:
                        _apply_capped_adjustment(
                            sector_results,
                            {code: _um_adj for code in sector_results},
                            adj_key        = "us_macro_adjustment",
                            cap_config_key = "US_MACRO_MAX_SECTOR_ADJ",
                            label          = "US_MACRO",
                        )
                        print(f"  🇺🇸 US マクロ補正: 全業種 {_um_adj:+.2f}pt"
                              f"（スコア{score:.0f}/上限±{_um_cap:.0f}pt）")
                except Exception as _ume:
                    print(f"  ⚠️  USマクロ補正の適用エラー（スキップ）: {str(_ume)[:60]}")

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
                sector_res_all = dict(sector_res)   # 一次採点（全上場）は BOT 業種も点数に使う
                                # GAP-2 の格付けは補正途中のスコアで計算されている。
                                # スクリーナー対象を決める直前に、その時点のスコア順位で付け直す。
                sector_tier = results.get("sector_tier", {})
                if sector_res:
                    try:
                        sector_tier = _calc_sector_tier(sector_res, results)
                        results["sector_tier"] = sector_tier
                        for _code, _t in sector_tier.items():
                            if isinstance(sector_res.get(_code), dict):
                                sector_res[_code]["sector_tier"] = _t
                    except Exception as _tre:
                        print(f"    ⚠️  sector_tier 再計算エラー（前回値を使用）: {_tre}")

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
                    all_sector_results = sector_res_all,
                )
                results["screener"] = screen_result
                results["screener_universe"] = screen_result.get("universe")
                try:   # 基本点を先に記録する（以降の加減点と区別するため）
                    import score_trail as _st_base
                    _st_base.checkpoint("stock_screener")
                except Exception:
                    pass
                                # 候補に審議KPI 9項目を付ける（表示専用・点数には使わない）
                try:
                    import candidate_kpi as _ckpi
                    results["candidate_kpi"] = _ckpi.attach_kpis(cfg, results)
                except Exception as _ck_e:
                    print(f"  ⚠️  candidate_kpi エラー（スキップ）: {str(_ck_e)[:80]}")

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
                        print("    ✅ セクター振り分け完了")
                except Exception as _se:
                    print(f"    ⚠️  セクター振り分けエラー: {_se}")
                _serr = screen_result.get("sector_errors") or []
                _uerr = screen_result.get("universe_error")
                _usum = screen_result.get("universe") or {}
                logger.add("stock_screener", "warn" if (_serr or _uerr or _usum.get("bq_write_error")) else "success",
                           f"通過:{screen_result.get('pass_count',0)}銘柄"
                           + (f" / 一次採点{_usum.get('universe_count')}社→二次{_usum.get('stage2_n')}社" if _usum else "")
                           + (f" / 一次採点失敗（従来経路で続行）: {_uerr}" if _uerr else "")
                           + (f" / 一次採点のBQ書込失敗: {_usum.get('bq_write_error')}" if _usum.get("bq_write_error") else "")
                           + (f" / 例外で飛ばした業種{len(_serr)}: "
                              + "; ".join(f"{e['sector']} {e['where']}" for e in _serr)[:200]
                              if _serr else ""))

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
                                # ── [FE-5] PEAD Monitor 日次実行 ────────────────
                if _in_mode("pead_monitor"):
                    try:
                        _pead = _load_local_module(
                            "pead_monitor",
                            "/content/pead_monitor.py",
                            cfg.DRIVE_BASE / "pead_monitor.py",
                        )
                        if _pead:
                            pead_result = _pead.run_pead_monitor(cfg, results)
                            results["pead"] = pead_result
                            logger.add(
                                "pead_monitor", "success",
                                f"追跡{pead_result.get('total_tracked',0)}件 "
                                f"アラート{len(pead_result.get('alert_candidates',[]))}件",
                            )
                    except Exception as _peade:
                        logger.add("pead_monitor", "error", str(_peade)[:100])
                        print(f"  ⚠️  PEAD Monitorエラー（スキップ）: {str(_peade)[:60]}")

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
                                if t in comps_cache:
                                    _sc.apply_valuation_to_candidate(
                                        c,
                                        comps_cache[t],
                                        c.get("dcf_bonus")
                                        if "dcf" in c.get("valuation_sources", [])
                                        else None,
                                        cfg,
                                    )
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
                                if t in dcf_cache:
                                    bonus = _dcf.get_dcf_bonus(t, dcf_cache)
                                    _fresh_comps = (
                                        results.get("comps_analyzer", {})
                                        .get("results", {}).get(t)
                                    )
                                    if (_fresh_comps is None
                                            and "comps" in c.get("valuation_sources", [])
                                            and c.get("comps_score") is not None):
                                        _fresh_comps = {"comps_score": c["comps_score"]}
                                    _sc.apply_valuation_to_candidate(
                                        c, _fresh_comps, bonus, cfg
                                    )
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
                                        # 対象はスクリーナーが取得した全候補。分析直後に当日の結果で高確率/ネガティブ判定をやり直す
                    _es_tickers = [c.get("ticker") for c in screen_result.get("candidates", [])
                                   if c.get("ticker")] or passed_tickers
                    if _es and getattr(cfg, "ENABLE_EARNINGS_SURPRISE", True) and _es_tickers:
                        es_result = _es.run_earnings_surprise(cfg, _es_tickers)
                        results["earnings_surprise"] = es_result
                        _flags = _es.apply_surprise_flags(
                            screen_result.get("candidates", []), _es._load_cache(cfg), cfg)
                        print(f"    🎯 決算サプライズ判定: 高確率{_flags['high_prob']}社 / "
                              f"ネガティブ{_flags['negative']}社")
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
                        # ── [D-1] 投資判断カード（自然言語サマリー）────────
                        if getattr(cfg, "ENABLE_INVESTMENT_CARD", True):
                            try:
                                _generate_investment_cards(
                                    screen_result.get("candidates", []),
                                    results, cfg,
                                )
                            except Exception as _d1e:
                                print(f"  ⚠️  [D-1] 投資判断カードエラー: {_d1e}")
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

        # ── [FE-6] Portfolio Diversity Filter（WEEKLY）────────
        # results["screener"] を読むのでスクリーニングの後に置く。
    if _in_mode("portfolio_diversity_filter"):
        try:
            _pdf = _load_local_module(
                "portfolio_diversity_filter",
                "/content/portfolio_diversity_filter.py",
                cfg.DRIVE_BASE / "portfolio_diversity_filter.py",
            )
            if _pdf:
                div_result = _pdf.run_portfolio_diversity_filter(cfg, results)
                results["diversity"] = div_result
                hhi_str = f"HHI={div_result.get('hhi', 0):.3f}"
                alert   = "⚠️ アラート" if div_result.get("should_alert") else "正常"
                logger.add(
                    "portfolio_diversity_filter", "success",
                    f"{hhi_str} [{div_result.get('hhi_status','?')}] {alert}",
                )
        except Exception as _pdfe:
            logger.add("portfolio_diversity_filter", "error", str(_pdfe)[:100])
            print(f"  ⚠️  Portfolio Diversityエラー（スキップ）: {str(_pdfe)[:60]}")

# ── [Policy Ph1] 議員取引監視（週2回: 火・金）──────────
    if getattr(cfg, "ENABLE_POLITICIAN_MONITOR", True):
       _run_days = getattr(cfg, "POLITICIAN_RUN_WEEKDAYS", (1, 4))
              # ここでは _dt を使わない（関数内の別ブロックで `from datetime import date as _dt` に
              #   束縛されており、date には .now() が無い）。モジュールレベルの datetime を使う。
       if datetime.now().weekday() in _run_days:
        try:
            _today_dow = datetime.now().weekday()   # 0=月 1=火 ... 4=金
            if _today_dow in (1, 4):           # 火曜=1 / 金曜=4
                _pm = _load_local_module(
                    "politician_monitor",
                    "/content/politician_monitor.py",
                    cfg.DRIVE_BASE / "politician_monitor.py",
                )
                if _pm:
                    pm_result = _pm.run_politician_monitor(cfg)
                    results["politician_monitor"] = pm_result
                    # JP業種への波及シグナルを jp_us_bridge に注入
                    _bridge_mod = _load_local_module(
                        "jp_us_sector_bridge",
                        "/content/jp_us_sector_bridge.py",
                        cfg.DRIVE_BASE / "jp_us_sector_bridge.py",
                    )
                    if _bridge_mod and hasattr(_bridge_mod, "apply_politician_signal_to_jp"):
                        _pol_adj = _bridge_mod.apply_politician_signal_to_jp(pm_result)
                        if _pol_adj:
                            _br = results.get("jp_us_bridge", {})
                            _br["politician_adjustments"] = _pol_adj
                            results["jp_us_bridge"] = _br
                    n_high = pm_result.get("stats", {}).get("high_score_count", 0)
                    logger.add(
                        "politician_monitor", "success",
                        f"新規:{pm_result.get('stats',{}).get('new_count',0)}件 "
                        f"高スコア:{n_high}件",
                    )
                else:
                    print("  ⚠️  politician_monitor.py が見つかりません")
            else:
                print("  ⏩ 議員取引監視: 本日は実行対象外（火・金のみ）")
        except Exception as _pme:
            logger.add("politician_monitor", "error", str(_pme)[:100])
            print(f"  ⚠️  議員取引監視エラー（スキップ）: {str(_pme)[:80]}")
        
        # ── [17.7] institutional_13f_monitor（Policy Ph3）───────
        # DAILY_MODULES のキーで判定し、取得頻度は _is_quarter_filing_period()（1/4/7/10月15日〜末日）が絞る。
        # それ以外の日はキャッシュを利用（SEC EDGAR 負荷軽減）。
    if getattr(cfg, "ENABLE_INSTITUTIONAL_13F", True) and _in_mode("institutional_13f"):
        try:
            _i13f = _load_local_module(
                "institutional_13f_monitor",
                "/content/institutional_13f_monitor.py",
                cfg.DRIVE_BASE / "institutional_13f_monitor.py",
            )
            if _i13f:
                # 四半期提出期間かどうかを判定
                _is_qend = _i13f._is_quarter_filing_period()
                _i13f_result = _i13f.run_institutional_13f_monitor(
                    cfg,
                    results,
                    is_quarter_end = _is_qend,
                )
                results["institutional_13f"] = _i13f_result

                # institutional_signal_scorer を続けて実行
                _iss = _load_local_module(
                    "institutional_signal_scorer",
                    "/content/institutional_signal_scorer.py",
                    cfg.DRIVE_BASE / "institutional_signal_scorer.py",
                )
                if _iss:
                    _iss_result = _iss.run_institutional_signal_scorer(
                        _i13f_result, results, cfg
                    )
                    results["institutional_scorer"] = _iss_result

                    # L3 Override 発動ログ
                    if _iss_result.get("l3_triggered"):
                        logger.add(
                            "institutional_13f", "l3_override",
                            f"L3発動: {_iss_result['override_tickers']} "
                            f"({len(_iss_result['override_sectors'])}業種)",
                        )
                    else:
                        logger.add(
                            "institutional_13f", "success",
                            f"quarter={_i13f_result.get('quarter','')} / "
                            f"changes={_i13f_result.get('total_changes',0)}件 / "
                            f"{_iss_result.get('summary','')}",
                        )
                else:
                    logger.add(
                        "institutional_13f", "success",
                        f"scorer未配置 / "
                        f"changes={_i13f_result.get('total_changes',0)}件",
                    )
            else:
                print("  ⚠️  institutional_13f_monitor.py が見つかりません")
        except Exception as _i13fe:
            logger.add("institutional_13f", "error", str(_i13fe)[:100])
            print(f"  ⚠️  institutional_13f エラー（スキップ）: "
                  f"{str(_i13fe)[:80]}")

        # ── [17.7] congress_monitor 実行（Policy Ph4）──────────
        # policy_signal_applier が results["congress_monitor"] を読むので、その前に実行する。
        # 法案進行は低頻度のため WEEKLY_MODULES 登録（L4キャッシュ減衰で日次適用）。
    if (getattr(cfg, "ENABLE_CONGRESS_MONITOR", True)
            and _in_mode("congress_monitor")
            and not results.get("emergency_override")):
        try:
            _cm = _load_local_module(
                "congress_monitor",
                "/content/congress_monitor.py",
                cfg.DRIVE_BASE / "congress_monitor.py",
            )
            if _cm:
                cm_result = _cm.run_congress_monitor(cfg, results)
                results["congress_monitor"] = cm_result or {}
                _cmr = cm_result or {}
                logger.add(
                    "congress_monitor",
                    "success" if _cmr.get("fetched_count") else "warn",
                    f"取得{_cmr.get('fetched_count', 0)}件 / 影響法案{_cmr.get('bill_count', 0)}件 "
                    f"| ブースト{len(_cmr.get('sector_boosts', {}))}業種",
                )
        except Exception as _cme:
            logger.add("congress_monitor", "error", str(_cme)[:100])
            print(f"  ⚠️  congress_monitor エラー（スキップ）: {str(_cme)[:60]}")

        # ── [17.8] policy_signal_applier 日次実行（Policy Ph2） ──
        # politician_monitor / screener の直後に実行。
        # EMERGENCY_OVERRIDE_FLAG が True の場合はスキップ。
    if _in_mode("policy_signal_applier") and not results.get("emergency_override"):
        try:
            _psa = _load_local_module(
                "policy_signal_applier",
                "/content/policy_signal_applier.py",
                cfg.DRIVE_BASE / "policy_signal_applier.py",
            )
            if _psa:
                # politician_signal_scorer を先に実行してスコアを生成
                try:
                    _pss = _load_local_module(
                        "politician_signal_scorer",
                        "/content/politician_signal_scorer.py",
                        cfg.DRIVE_BASE / "politician_signal_scorer.py",
                    )
                    if _pss:
                        _pss_result = _pss.run_politician_signal_scorer(
                            results.get("politician_monitor", {}),
                            results,
                            cfg,
                        )
                        results["politician_scorer"] = _pss_result
                except Exception as _psse:
                    print(f"    ⚠️  politician_signal_scorer エラー: {str(_psse)[:60]}")

                psa_result = _psa.run_policy_signal_applier(results, cfg)
                results["policy_signals"] = psa_result
                logger.add(
                    "policy_signal_applier", "success",
                    psa_result.get("summary", ""),
                )
        except Exception as _psae:
            logger.add("policy_signal_applier", "error", str(_psae)[:100])
            print(f"  ⚠️  policy_signal_applierエラー（スキップ）: {str(_psae)[:80]}")
    elif results.get("emergency_override"):
        print("  ⏩ policy_signal_applier: EMERGENCY停止中のためスキップ")
                # ── [S3-1] sector_tier 分類ログ ─────────────────
            # _calc_sector_tier() は GAP-2 で計算済み。ここでは deep_dive_trigger への接続と詳細ログ出力を行う。
    if getattr(cfg, "ENABLE_S3_SECTOR_TIER_LOG", True):
        try:
            _tier_map = results.get("sector_tier", {})
            if _tier_map:
                _top = [c for c, t in _tier_map.items() if t == "TOP"]
                _mid = [c for c, t in _tier_map.items() if t == "MID"]
                _bot = [c for c, t in _tier_map.items() if t == "BOT"]
                print(
                    f"\n  📊 [S3-1] sector_tier 分類: "
                    f"TOP={len(_top)} / MID={len(_mid)} / BOT={len(_bot)}"
                )
                if _top:
                    print(f"    TOP業種: {_top[:5]}")
                if _bot:
                    print(f"    BOT業種: {_bot[:5]}")
        except Exception as _s3e:
            print(f"  ⚠️  [S3-1] sector_tier ログエラー（スキップ）: "
                  f"{str(_s3e)[:60]}")

        # ── [S3-4] deep_dive_trigger ────────────────────────
        # confidence 低下・screener 順位急変を検出し、EDINET 深掘り対象を cfg.EDINET_DEEP_DIVE_TICKERS に注入する。
        # 受け手の edinet_rotation_manager（EDINETローテ）は廃止済みのため、DAILY_MODULES に未登録＝停止中。
    if getattr(cfg, "ENABLE_DEEP_DIVE_TRIGGER", True) and _in_mode("deep_dive_trigger"):
        try:
            _ddt = _load_local_module(
                "deep_dive_trigger",
                "/content/deep_dive_trigger.py",
                cfg.DRIVE_BASE / "deep_dive_trigger.py",
            )
            if _ddt:
                _dd_result = _ddt.run_deep_dive_trigger(cfg, results)
                results["deep_dive"] = _dd_result
                if _dd_result.get("total", 0) > 0:
                    logger.add(
                        "deep_dive_trigger", "success",
                        f"深掘り{_dd_result['total']}銘柄: "
                        f"confidence低下{len(_dd_result['confidence_triggers'])}件 / "
                        f"rank急変{len(_dd_result['rank_triggers'])}件",
                    )
                else:
                    logger.add("deep_dive_trigger", "success", "トリガーなし")
            else:
                print("  ⚠️  deep_dive_trigger.py が見つかりません")
        except Exception as _dde:
            logger.add("deep_dive_trigger", "error", str(_dde)[:100])
            print(f"  ⚠️  deep_dive_trigger エラー（スキップ）: "
                  f"{str(_dde)[:80]}")

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
            # ── [B-1] 決算短信開示銘柄をEDINETの最優先に追加 ────
            if getattr(cfg, "ENABLE_B1_TDNET_EDINET", True):
                _kessan_tickers = []
                                # 決算短信カテゴリは config（B1_KESSAN_CATEGORIES）から読む。
                _kessan_cats = set(getattr(
                    cfg, "B1_KESSAN_CATEGORIES",
                    ["kessan", "financial_results", "決算短信", "earnings"]))
                # TDnet の決算短信（kessan）銘柄を収集
                for _item in tdnet_result.get("disclosures", []):
                    if isinstance(_item, dict):
                        _cat = _item.get("category", "")
                        _t   = _item.get("ticker", "") or _item.get("code", "")
                        # 決算短信カテゴリのみ対象
                        if _cat in _kessan_cats and _t:
                            _ticker_jp = (
                                f"{_t}.T" if not str(_t).endswith(".T")
                                and len(str(_t)) == 4 else str(_t)
                            )
                            _kessan_tickers.append(_ticker_jp)

                if _kessan_tickers:
                    # cfg.EDINET_WATCHLIST_TICKERS の先頭に挿入
                    _existing_wl = list(getattr(cfg, "EDINET_WATCHLIST_TICKERS", []))
                    cfg.__dict__["EDINET_WATCHLIST_TICKERS"] = (
                        list(dict.fromkeys(_kessan_tickers + _existing_wl))[:20]
                    )
                    print(
                        f"    📌 [B-1] 決算短信銘柄→EDINET優先: "
                        f"{_kessan_tickers[:5]}"
                    )
                    logger.add(
                        "b1_tdnet_edinet", "success",
                        f"決算短信{len(_kessan_tickers)}社をEDINET最優先に設定",
                    )
        except Exception as e:
            logger.add("tdnet_monitor", "error", str(e)[:100])
            print(f"  ⚠️  TDnetモニターエラー: {str(e)[:80]}")
    else:
        print("  ⏩ TDnetモニター: スキップ（ENABLE_TDNET_MONITOR=False）")
    
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
                # 有報「役員の状況」から経営トップと就任日を読む（書類が変わった会社だけ・1日上限あり）
                if getattr(cfg, "ENABLE_OFFICER_EXTRACTOR", True):
                    try:
                        import officer_extractor as _oe
                        _hold = []
                        try:
                            import csv as _csv_h
                            with open(Path(getattr(cfg, "PROJECT_DIR", ".")) / "data" / "portfolio_holdings.csv",
                                      encoding="utf-8-sig") as _fh:
                                _hold = [r["ticker"] for r in _csv_h.DictReader(_fh)]
                        except OSError:
                            pass
                        _oe_stats = _oe.run(cfg, _ce_tickers + _hold)
                        logger.add("officer_extractor", "success" if not _oe_stats.get("error") else "warn",
                                   f"対象{_oe_stats['targets']}社 / 読取{_oe_stats['ok']}社 / "
                                   f"トップ不明{_oe_stats['no_top']} / 失敗{_oe_stats['error']}")
                    except Exception as _oee:
                        logger.add("officer_extractor", "error", f"{type(_oee).__name__}: {str(_oee)[:80]}")
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

    # ── 政策DB（公的支出・経済安保）の取り込み ─────────────
    # 保有銘柄を先頭に、スクリーナー候補、上場全社の順で個社の公的支出を1日の予算が尽きるまで取る。
    # 朝の取得 workflow（fetch_stages.jp_filings）が取っていれば、その結果（apply_external で写したもの）を使う。
    if getattr(cfg, "ENABLE_SEISAKUDB", True) and _in_mode("seisakudb") and "seisakudb" not in results:
        try:
            import seisakudb_ingestor as _sk
            _sk_hold = []
            try:
                import csv as _csv_s
                with open(Path(getattr(cfg, "PROJECT_DIR", ".")) / "data" / "portfolio_holdings.csv",
                          encoding="utf-8-sig") as _fh:
                    _sk_hold = [r["ticker"] for r in _csv_s.DictReader(_fh)]
            except OSError:
                pass
            _sk_cands = [c["ticker"] for c in (results.get("screener", {}) or {}).get("candidates", [])
                         if c.get("ticker")]
            _sk_res = _sk.run(cfg, _sk_hold + _sk_cands)
            results["seisakudb"] = _sk_res
            if _sk_res.get("skipped"):
                logger.add("seisakudb", "skipped", f"スキップ（{_sk_res.get('reason', '')}）")
            else:
                _st = _sk_res.get("stopped") or ""
                logger.add("seisakudb", "warn" if (_sk_res.get("errors") or (_st and _st != "budget")) else "success",
                           f"直近{_sk_res.get('recent', 0)}件 / 個社{_sk_res.get('companies_ok', 0)}社 / "
                           f"経済安保{_sk_res.get('certs', 0)}件 / API {_sk_res.get('calls', 0)}回"
                           + (f" / 停止: {_st}" if _st else ""))
        except Exception as _ske:
            logger.add("seisakudb", "error", f"{type(_ske).__name__}: {str(_ske)[:80]}")
    # ── 公的支出の特徴量（保有+候補）を候補に付ける ─────────────
    # 加点は ENABLE_PUBLIC_FUNDING_BONUS が真のときだけ。偽のときは表示用の付与のみ。
    if getattr(cfg, "ENABLE_PUBLIC_FUNDING_FEATURES", True) and _in_mode("seisakudb"):
        try:
            import public_funding_features as pff
            import stock_screener as _sc_pf
            _pf_hold = []
            try:
                import csv as _csv_p
                with open(Path(getattr(cfg, "PROJECT_DIR", ".")) / "data" / "portfolio_holdings.csv",
                          encoding="utf-8-sig") as _fh:
                    _pf_hold = [r["ticker"] for r in _csv_p.DictReader(_fh)]
            except OSError:
                pass
            _pf_cands = (results.get("screener", {}) or {}).get("candidates", [])
            _pf_tk = list(dict.fromkeys(_pf_hold + [c["ticker"] for c in _pf_cands if c.get("ticker")]))
            _pf_feats = pff.build_features(cfg, _pf_tk)
            results["public_funding"] = _pf_feats
            _sc_pf.attach_public_funding(_pf_cands, _pf_feats, cfg)
            logger.add("public_funding_features", "success",
                       f"{len(_pf_feats)}銘柄 / 加点{'ON' if getattr(cfg, 'ENABLE_PUBLIC_FUNDING_BONUS', False) else 'OFF'}")
        except Exception as _pfe:
            logger.add("public_funding_features", "error", f"{type(_pfe).__name__}: {str(_pfe)[:80]}")
        # ── [I-5] 中期経営計画達成率トラッカー ─────────────
    if getattr(cfg, "ENABLE_MIDTERM_PLAN_TRACKER", True) and _in_mode("screener"):
        try:
            _mt = _load_local_module(
                "midterm_plan_tracker",
                "/content/midterm_plan_tracker.py",
                cfg.DRIVE_BASE / "midterm_plan_tracker.py",
            )
            if _mt:
                                # 引数は cfg のみ（キャッシュ駆動）
                mt_result = _mt.run_midterm_plan_tracker(
                    cfg          = cfg,
                    force_refresh = False,
                )
                results["midterm_tracker"]        = mt_result
                results.setdefault("wave7_trackers", {})["midterm"] = mt_result
                _mt_plans = mt_result.get("plans", {})
                _at_risk  = mt_result.get("stats", {}).get("at_risk_count", 0)
                _high     = mt_result.get("stats", {}).get("high_count", 0)
                logger.add(
                    "midterm_tracker", "success",
                    f"追跡{len(_mt_plans)}社 "
                    f"| HIGH={_high} "
                    f"| AT_RISK={_at_risk}",
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
        
        # ── 出口戦略エンジン ──────────────────────
        # 投資哲学の売却ルールを日次評価 → 接近/発火アラート＋注文票生成。
        # 発注は利用者が手動（D-01）。アラート通知より前に実行し
        # results["exit_alerts"] を alert_notifier のブリーフに載せる。
    if getattr(cfg, "ENABLE_EXIT_ENGINE", False):
        try:
            _ex = _load_local_module(
                "exit_rule_engine",
                "/content/exit_rule_engine.py",
                cfg.DRIVE_BASE / "exit_rule_engine.py",
            )
            if _ex:
                ex_result = _ex.run_exit_rule_engine(cfg, results)
                results["exit_alerts"] = ex_result
                _fires = [a for a in ex_result.get("alerts", [])
                          if a.get("level") == "fire"]
                logger.add("exit_rule_engine", "success",
                           f"監視:{ex_result.get('checked',0)}銘柄 "
                           f"発火:{len(_fires)}件 "
                           f"注文票:{len(ex_result.get('tickets',[]))}件")
            else:
                print("  ⚠️  exit_rule_engine.py が見つかりません")
                logger.add("exit_rule_engine", "warn", "exit_rule_engine.py not found")
        except Exception as e:
            logger.add("exit_rule_engine", "error", str(e)[:100])
            print(f"  ⚠️  出口戦略エンジンエラー: {str(e)[:80]}")
    else:
        print("  ⏩ 出口戦略エンジン: スキップ（ENABLE_EXIT_ENGINE=False）")

        # ── データ鮮度SLA検査 ────────────────────
    if getattr(cfg, "ENABLE_DATA_SLA", False):
        try:
            _dr = _load_local_module(
                "data_reliability",
                "/content/data_reliability.py",
                cfg.DRIVE_BASE / "data_reliability.py",
            )
            if _dr:
                sla_report = _dr.run_data_sla_check(cfg, results)
                results["data_sla_report"] = sla_report
                _vc = sla_report.get("violation_count", 0)
                logger.add("data_sla_check",
                           "success" if _vc == 0 else "warn",
                           f"SLA違反:{_vc}件 / 検査:{sla_report.get('checked',0)}対象")
            else:
                print("  ⚠️  data_reliability.py が見つかりません")
                logger.add("data_sla_check", "warn", "data_reliability.py not found")
        except Exception as e:
            logger.add("data_sla_check", "error", str(e)[:100])
            print(f"  ⚠️  SLA検査エラー: {str(e)[:80]}")
    else:
        print("  ⏩ 鮮度SLA検査: スキップ（ENABLE_DATA_SLA=False）")

        # ── ドローダウン制御 ──────────────────────
        # ポートフォリオ投資部分のDDでNORMAL/CAUTION/DEFENSEを遷移。
        # 売却強制なし（推奨のみ=D-01）。results["dd_state"]をブリーフ⑤へ。
    if getattr(cfg, "ENABLE_DD_CONTROL", False):
        try:
            _dd = _load_local_module(
                "dd_controller",
                "/content/dd_controller.py",
                cfg.DRIVE_BASE / "dd_controller.py",
            )
            if _dd:
                dd_result = _dd.run_dd_controller(cfg, results)
                results["dd_state"] = dd_result
                if not dd_result.get("skipped"):
                    logger.add("dd_controller", "success",
                               f"{dd_result.get('state','?')} "
                               f"dd={dd_result.get('dd',0):.1%} "
                               f"{'★遷移' if dd_result.get('transitioned') else ''}")
            else:
                print("  ⚠️  dd_controller.py が見つかりません")
                logger.add("dd_controller", "warn", "dd_controller.py not found")
        except Exception as e:
            logger.add("dd_controller", "error", str(e)[:100])
            print(f"  ⚠️  DD制御エラー: {str(e)[:80]}")
    else:
        print("  ⏩ DD制御: スキップ（ENABLE_DD_CONTROL=False）")

        # ── ペーパートレード仮想口座 ───────────────
        # 前日注文を当日始値で仮想約定 → equity更新 → 翌日注文生成。
        # 実発注は存在しない（D-01）。results["paper_summary"] を
        # alert_notifier のブリーフ⑥に載せるため通知より前に実行。
    if getattr(cfg, "ENABLE_PAPER_TRADING", False):
        try:
            _pb = _load_local_module(
                "paper_broker",
                "/content/paper/paper_broker.py",
                cfg.DRIVE_BASE / "paper" / "paper_broker.py",
            )
            if _pb:
                pb_result = _pb.run_paper_trading(cfg, results)
                results["paper_summary"] = pb_result
                _pb_accounts = [v for v in pb_result.values()
                                if isinstance(v, dict)]
                logger.add("paper_broker", "success",
                           f"戦略:{len(_pb_accounts)}口座 "
                           f"約定:{sum(v.get('fills', 0) for v in _pb_accounts)}件 "
                           f"新規注文:{sum(v.get('new_orders', 0) for v in _pb_accounts)}件")
                # 口座ごとの日次VaR95%（保有銘柄×過去1年の騰落）。リスク管制ボードが var_positions.json を読む
                try:
                    from paper import position_var as _pv
                    _pv.run(cfg, _pb.load_accounts(cfg), _pb._paper_dir(cfg))
                    logger.add("position_var", "success", "var_positions.json")
                except Exception as _pve:
                    logger.add("position_var", "error", str(_pve)[:100])
                    print(f"  ⚠️  VaR（保有銘柄ベース）エラー: {str(_pve)[:80]}")
            else:
                print("  ⚠️  paper/paper_broker.py が見つかりません")
                logger.add("paper_broker", "warn", "paper_broker.py not found")
        except Exception as e:
            logger.add("paper_broker", "error", str(e)[:100])
            print(f"  ⚠️  ペーパートレードエラー: {str(e)[:80]}")
    else:
        print("  ⏩ ペーパートレード: スキップ（ENABLE_PAPER_TRADING=False）")

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

    # ── 技術マップのまとめ（スクリーナー通過銘柄 × 技術ノード × 熱度）。メールの「🧭 技術ノードの熱度」節が読む ──
    if getattr(cfg, "ENABLE_TECH_MAP_DIGEST", True) and _in_mode("tech_map"):
        try:
            import tech_profile as _tp
            results["tech_map"] = _tp.run_digest(cfg, results)
            _tm = results["tech_map"]
            logger.add("tech_map", "success",
                       f"候補{_tm.get('candidates', 0)}社のうち技術ノード付き{_tm.get('covered', 0)}社・"
                       f"ノード{len(_tm.get('nodes') or [])}件")
        except Exception as exc:
            logger.add("tech_map", "error", str(exc)[:100])

        # ── アラート通知（米国株スクリーナーの後。メールの米国株サマリーがその結果を読む）────────────────
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

        # ── 銘柄スナップショットをBQへ日次蓄積（両スクリーナー直後）──
        # 銘柄mdにしか出ない指標（technical系・FCFマージン・D/E・自己資本比率・配当利回り・
        # sector_score・technical_score）の正本。md生成（上位30銘柄・JPのみ）には依存せず、通過銘柄の全件を残す。
    if getattr(cfg, "ENABLE_STOCK_SNAPSHOT", True) and _in_mode("screener"):
        try:
            _ssw = _load_local_module(
                "stock_snapshot_writer",
                "/content/stock_snapshot_writer.py",
                cfg.DRIVE_BASE / "stock_snapshot_writer.py",
            )
            if _ssw:
                _snap_res = _ssw.run_stock_snapshot_writer(cfg, results)
                results["stock_snapshot"] = _snap_res
                if _snap_res.get("skipped"):
                    logger.add("stock_snapshot", "skipped",
                               str(_snap_res.get("reason", ""))[:80])
                else:
                    logger.add(
                        "stock_snapshot",
                        "success" if _snap_res.get("inserted") else "warn",
                        f"JP{_snap_res.get('jp', 0)} / "
                        f"US{_snap_res.get('us', 0)} / "
                        f"計{_snap_res.get('rows', 0)}行 / "
                        f"BQ={_snap_res.get('inserted', False)}",
                    )
            else:
                logger.add("stock_snapshot", "warn",
                           "stock_snapshot_writer.py が見つかりません")
        except Exception as _sswe:
            logger.add("stock_snapshot", "error", str(_sswe)[:100])
            print(f"  ⚠️  スナップショット格納エラー（スキップ）: {str(_sswe)[:60]}")

        # ── 推奨のその後（通過銘柄の 5・20・60 取引日後の騰落）を stock_snapshot の後に更新 ──
    if getattr(cfg, "ENABLE_PICK_OUTCOMES", True) and _in_mode("screener"):
        try:
            import pick_outcomes as _po
            _po_res = _po.update(cfg)
            results["pick_outcomes"] = _po_res
            if _po_res.get("skipped"):
                logger.add("pick_outcomes", "skipped", str(_po_res.get("reason", ""))[:80])
            elif _po_res.get("error"):
                logger.add("pick_outcomes", "error", str(_po_res["error"])[:100])
            else:
                logger.add("pick_outcomes", "success",
                           f"{_po_res.get('since')} 以降 {_po_res.get('rows', 0)}行 / {_po_res.get('gb_billed')}GB")
        except Exception as _poe:
            logger.add("pick_outcomes", "error", str(_poe)[:100])
            print(f"  ⚠️  推奨のその後の更新エラー（スキップ）: {str(_poe)[:60]}")

        # ── [Size Ph1] 規模分類マスター生成（両スクリーナーの直後）────
        #   results["screener"]（stock_screener）と results["us_screener"]
        #   （us_stock_screener）の両方が確定してから実行する必要がある。
    if _in_mode("size_tier_classifier"):
        try:
            _stc = _load_local_module(
                "size_tier_classifier",
                "/content/size_tier_classifier.py",
                cfg.DRIVE_BASE / "size_tier_classifier.py",
            )
            if _stc:
                stc_result = _stc.run_size_tier_classifier(cfg, results)
                results["size_tier_master"] = stc_result.get("master", {})
                _stc_stats = stc_result.get("stats", {})
                _td = _stc_stats.get("tier_distribution", {})
                if stc_result.get("skipped"):
                    # 全0を success として記録し続ける事象（X-1）の再発検知点
                    logger.add(
                        "size_tier_classifier", "skipped",
                        str(stc_result.get("reason", "master未生成"))[:100],
                    )
                else:
                    logger.add(
                        "size_tier_classifier", "success",
                        f"JP:{_stc_stats.get('jp_total', 0)} / "
                        f"US:{_stc_stats.get('us_total', 0)} / "
                        f"Large:{_td.get('Large', 0)} "
                        f"Mid:{_td.get('Mid', 0)} "
                        f"Small:{_td.get('Small', 0)} "
                        f"Micro:{_td.get('Micro', 0)} "
                        f"Nano:{_td.get('Nano', 0)}",
                    )
            else:
                print("  ⚠️  size_tier_classifier.py が見つかりません")
        except Exception as _stce:
            logger.add("size_tier_classifier", "error", str(_stce)[:100])
            print(
                f"  ⚠️  規模分類マスター生成エラー（スキップ）: {str(_stce)[:80]}"
            )
    else:
        print("  ⏩ 規模分類マスター: スキップ（週次以外）")

        # ── [US-1] US財務DBローテーター ────────────────────────
    if getattr(cfg, "ENABLE_US_FUNDAMENTALS", True) and _in_mode("us_fundamentals") and not source_ingest_done["us"]:
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
                # 日本株スクリーナーの通過銘柄に、米国株スクリーナーの上位を足す。
                # 日本株だけを渡すと米国のインサイダー取得が0銘柄になる。米国株は us_screener が同じ上位の
                # Form 4 を取得済みで、キャッシュを共有するため取得回数は増えない。
                screener_tickers = [
                    c["ticker"]
                    for c in results.get("screener", {}).get("candidates", [])
                    if c.get("pass_filters")
                ]
                _us = results.get("us_screener") or {}
                screener_tickers += [
                    c["ticker"] for c in ((_us.get("top_picks") or []) + (_us.get("candidates") or []))
                    if isinstance(c, dict) and c.get("ticker") and c["ticker"] not in screener_tickers
                ][:int(getattr(cfg, "US_INSIDER_MAX_TICKERS", 40))]
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
            # ── [⑤] 重みを更新した回（月末の週）だけ: 監査ログ生成 ─────────────
            # 月次戦略レビューの④がこのファイル（learning_audit_YYYY-MM.json）を読む。
            _ro_res = results.get("rule_optimizer") or {}
            if _ro_res.get("updated"):
                try:
                    _al = _load_local_module(
                        "learning_audit_log",
                        "/content/learning_audit_log.py",
                        cfg.DRIVE_BASE / "learning_audit_log.py",
                    )
                    if _al:
                        audit = _al.generate_audit_log(cfg, _ro_res, results)
                        results["learning_audit"] = audit
                        logger.add("learning_audit", "alert" if audit.get("manual_review_needed") else "success",
                                   (audit.get("review_reason", "") or "監査ログ生成")[:80])
                except Exception as _ae:
                    logger.add("learning_audit", "error", str(_ae)[:100])
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
    # ── [RESEARCH-2] ファクター・クラウディング検知 ─────────────────
    if getattr(cfg, "ENABLE_FACTOR_CROWDING", True) and _in_mode("factor_crowding"):
        try:
            _fcm = _load_local_module(
                "factor_crowding_monitor",
                "/content/factor_crowding_monitor.py",
                cfg.DRIVE_BASE / "factor_crowding_monitor.py",
            )
            if _fcm:
                fcm_result = _fcm.run_factor_crowding_monitor(cfg, results)
                results["factor_crowding"] = fcm_result

                if not fcm_result.get("skipped"):
                    score = fcm_result.get("crowding_score", 0)
                    alert = fcm_result.get("crowding_alert", False)
                    unwind = fcm_result.get("unwind_alert", False)

                    logger.add(
                        "factor_crowding", "success" if not alert else "alert",
                        f"avg_corr={fcm_result.get('avg_correlation', 0):.3f} "
                        f"/ score={score} "
                        f"/ crowding={'🚨' if alert else '✅'} "
                        f"/ unwind={'🚨' if unwind else '-'}",
                    )
        except Exception as e:
            logger.add("factor_crowding", "error", str(e)[:100])
            print(f"  ⚠️  クラウディング検知エラー（スキップ）: {str(e)[:60]}")
    else:
        print("  ⏩ クラウディング検知: スキップ")
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
                    screener_result  = results.get("screener", {}),
                    results          = results,
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


    # ── スコア履歴・スクリーナー履歴の蓄積（Excel出力の有無に関わらず毎回実行）──
    try:
        if results.get("sectors"):
            _append_score_history(cfg, results["sectors"])
        if results.get("screener"):
                        # performance_tracker（ENABLE_PERFORMANCE_TRACKER）。
                        # ⚠️ False にすると screener_history.json の蓄積が止まり、
                        #    審議キュー・審議パケット①・self_evaluator・backtester・
                        #    投資ジャーナルの入力が全て枯れる。切る前に影響範囲を確認すること。
            if getattr(cfg, "ENABLE_PERFORMANCE_TRACKER", True):
                _pt_hist = _load_local_module(
                    "performance_tracker",
                    "/content/performance_tracker.py",
                    cfg.DRIVE_BASE / "performance_tracker.py",
                )
                if _pt_hist:
                    _pt_hist.append_screener_history(cfg, results["screener"])
                        # 選別の経路（点数の内訳・9指標・落ちた理由）を残す
            try:
                import screener_trace as _strace
                results["screener_trace"] = _strace.run(cfg, results)
            except Exception as _tr_e:
                print(f"  ⚠️  screener_trace エラー（スキップ）: {str(_tr_e)[:80]}")
            else:
                print("  ⏩ screener履歴の蓄積: スキップ"
                      "（ENABLE_PERFORMANCE_TRACKER=False）")
    except Exception as e:
        print(f"  ⚠️  履歴蓄積エラー（スキップ）: {str(e)[:60]}")
                # ── [Idea2-B] 業種ローテーション検出（signal_aggregatorの前に必須）──
    if _in_mode("sector_rotation_detector"):
        try:
            _srd = _load_local_module(
                "sector_rotation_detector",
                "/content/sector_rotation_detector.py",
                cfg.DRIVE_BASE / "sector_rotation_detector.py",
            )
            if _srd:
                srd_result = _srd.run_sector_rotation_detector(cfg)
                results["sector_rotation"] = srd_result
                summary = srd_result.get("summary", {})
                logger.add(
                    "sector_rotation_detector", "success",
                    f"上昇:{summary.get('rising_count',0)} "
                    f"ピーク:{summary.get('peak_count',0)} "
                    f"下落:{summary.get('falling_count',0)} "
                    f"底値:{summary.get('trough_count',0)} "
                    f"変化:{summary.get('change_count',0)}件",
                )
        except Exception as _srde:
            logger.add("sector_rotation_detector", "error", str(_srde)[:100])
            print(f"  ⚠️  ローテーション検出エラー（スキップ）: {str(_srde)[:80]}")

        # ── [FE-4] Ignition Detector ────────────────────────
        # sector_rotation_detector の直後に配置。
        # results["sector_rotation"] を参照するため、実行順序が前提条件。
        # run_ignition_detector(cfg, results=results) で results 辞書を渡し、
        # screener / signal_aggregator / sectors / arxiv の各結果を活用する。
    if _in_mode("ignition_detector"):
        try:
            _ign = _load_local_module(
                "ignition_detector",
                "/content/ignition_detector.py",
                cfg.DRIVE_BASE / "ignition_detector.py",
            )
            if _ign:
                ign_result = _ign.run_ignition_detector(cfg, results=results)
                results["ignition"] = ign_result
                logger.add(
                    "ignition_detector", "success",
                    f"発火:{ign_result.get('ignition_count', 0)}件 "
                    f"最高警戒:{ign_result.get('critical_count', 0)}件 "
                    f"| {ign_result.get('summary', '')}",
                )
        except Exception as _igne:
            logger.add("ignition_detector", "error", str(_igne)[:100])
            print(f"  ⚠️  Ignition Detectorエラー（スキップ）: {str(_igne)[:80]}")

# ── [T系] 技術クラスター生成パイプライン ──────────
    # 実行順序: technology_mapper → cluster_engine
    #           → emerging_detector → cluster_graph_exporter
    # sector_rotation_detector / ignition_detector の直後に配置。
    # _in_mode("sector_rotation_detector") と同じタイミングで実行する。
    if getattr(cfg, "ENABLE_TECHNOLOGY_MAPPER", True) and _in_mode("technology_mapper"):
        # ── Step T-1: technology_mapper ─────────────────────────
        try:
            import importlib as _imp_tm, sys as _sys_tm
            _sys_tm.path.insert(0, str(cfg.DRIVE_BASE))
            _tm = _imp_tm.import_module("technology_mapper")
            tm_result = _tm.run_technology_mapper(cfg, results)
            results["technology_mapper"] = tm_result
            if tm_result.get("success"):
                logger.add(
                    "technology_mapper", "success",
                    f"業種={tm_result.get('sector_count',0)} "
                    f"/ 技術ノード={tm_result.get('node_count',0)} "
                    f"/ キャッシュHIT={tm_result.get('cache_hit',False)}",
                )
            else:
                logger.add(
                    "technology_mapper", "error",
                    tm_result.get("error", "不明なエラー")[:80],
                )
        except Exception as _tme:
            logger.add("technology_mapper", "error", str(_tme)[:100])
            print(f"  ⚠️  technology_mapper エラー（スキップ）: {str(_tme)[:80]}")
    else:
        print("  ⏩ technology_mapper: スキップ")

    if getattr(cfg, "ENABLE_CLUSTER_ENGINE", True) and _in_mode("cluster_engine"):
        # ── Step T-2: cluster_engine ─────────────────────────────
        try:
            import importlib as _imp_ce, sys as _sys_ce
            _sys_ce.path.insert(0, str(cfg.DRIVE_BASE))
            _ce = _imp_ce.import_module("technology_cluster_engine")
            ce_result = _ce.run_cluster_engine(cfg, results)
            results["cluster_phase"] = results.get("cluster_phase") or {}
            if ce_result.get("success"):
                logger.add(
                    "cluster_engine", "success",
                    f"クラスター={ce_result.get('cluster_count',0)}件 "
                    f"/ フェーズ={ce_result.get('phase_summary',{})} "
                    f"/ キャッシュHIT={ce_result.get('cache_hit',False)}",
                )
            else:
                logger.add(
                    "cluster_engine", "error",
                    ce_result.get("error", "不明なエラー")[:80],
                )
        except Exception as _cee:
            logger.add("cluster_engine", "error", str(_cee)[:100])
            print(f"  ⚠️  cluster_engine エラー（スキップ）: {str(_cee)[:80]}")
    else:
        print("  ⏩ cluster_engine: スキップ")

    if getattr(cfg, "ENABLE_EMERGING_DETECTOR", True) and _in_mode("emerging_detector"):
        # ── Step T-3: emerging_detector ──────────────────────────
        try:
            import importlib as _imp_ed, sys as _sys_ed
            _sys_ed.path.insert(0, str(cfg.DRIVE_BASE))
            _ed = _imp_ed.import_module("emerging_technology_detector")
            ed_result = _ed.run_emerging_detector(cfg, results)
            if ed_result.get("success"):
                logger.add(
                    "emerging_detector", "success",
                    f"萌芽テーマ={ed_result.get('theme_count',0)}件 "
                    f"/ Top={ed_result.get('top_theme','なし')} "
                    f"/ キャッシュHIT={ed_result.get('cache_hit',False)}",
                )
            else:
                logger.add(
                    "emerging_detector", "error",
                    ed_result.get("error", "不明なエラー")[:80],
                )
        except Exception as _ede:
            logger.add("emerging_detector", "error", str(_ede)[:100])
            print(f"  ⚠️  emerging_detector エラー（スキップ）: {str(_ede)[:80]}")
    else:
        print("  ⏩ emerging_detector: スキップ")

    if getattr(cfg, "ENABLE_CLUSTER_GRAPH_EXPORT", True) and _in_mode("cluster_graph_exporter"):
        # ── Step T-4: cluster_graph_exporter ─────────────────────
        try:
            import importlib as _imp_cge, sys as _sys_cge
            _sys_cge.path.insert(0, str(cfg.DRIVE_BASE))
            _cge = _imp_cge.import_module("cluster_graph_exporter")
            cge_result = _cge.run_cluster_graph_exporter(cfg, results)
            if cge_result.get("success"):
                logger.add(
                    "cluster_graph_exporter", "success",
                    f"クラスター={cge_result.get('cluster_count',0)} "
                    f"/ 萌芽={cge_result.get('emerging_count',0)} "
                    f"/ BQ={cge_result.get('bq_written',0)}行 "
                    f"/ JSON={'OK' if cge_result.get('json_written') else 'NG'}",
                )
            else:
                logger.add(
                    "cluster_graph_exporter", "error",
                    cge_result.get("error", "不明なエラー")[:80],
                )
        except Exception as _cgee:
            logger.add("cluster_graph_exporter", "error", str(_cgee)[:100])
            print(
                f"  ⚠️  cluster_graph_exporter エラー（スキップ）: "
                f"{str(_cgee)[:80]}"
            )
    else:
        print("  ⏩ cluster_graph_exporter: スキップ")

        # ── [Idea2-A] JP×US 60日相関分析 ───────────────────────
    if _in_mode("jp_us_sector_correlation"):
        try:
            _juc = _load_local_module(
                "jp_us_sector_correlation",
                "/content/jp_us_sector_correlation.py",
                cfg.DRIVE_BASE / "jp_us_sector_correlation.py",
            )
            if _juc:
                juc_result = _juc.run_jp_us_sector_correlation(cfg)
                results["jp_us_sector_correlation"] = juc_result
                stats = juc_result.get("stats", {})
                logger.add(
                    "jp_us_sector_correlation", "success",
                    f"有意ペア:{stats.get('significant_count',0)}件 "
                    f"(|r|≥0.7) / "
                    f"データ:{stats.get('data_points',0)}日",
                )
        except Exception as _juce:
            logger.add("jp_us_sector_correlation", "error", str(_juce)[:100])
            print(f"  ⚠️  JP×US相関エラー（スキップ）: {str(_juce)[:80]}")
                        # ── rule_optimizer 縮退フラグ判定 ───
                # signal_aggregator 呼び出しの直前に実行することが必須。
                # evaluate_contraction_flag(results, cfg) は最新の results（VIX・市場幅等）を
                # 参照するため、options_monitor / market_breadth / geo_risk / bond_commodity が
                # 格納済みであるこのタイミングが最適。
                # 結果は results["rule_optimizer"]["contraction_flag"] に格納され、
                # signal_aggregator.calc_signal_score() / calc_total_signal_score() が参照する。
    if getattr(cfg, "ENABLE_SIGNAL_AGGREGATOR", True) and _in_mode("signal_aggregator"):
        try:
            _ro_cf = _load_local_module(
                "rule_optimizer",
                "/content/rule_optimizer.py",
                cfg.DRIVE_BASE / "rule_optimizer.py",
            )
            if _ro_cf and hasattr(_ro_cf, "evaluate_contraction_flag"):
                _contraction = _ro_cf.evaluate_contraction_flag(results, cfg)
                                # 格納先キーは config.CONTRACTION_RESULT_KEY から読む。
                _ck = str(getattr(cfg, "CONTRACTION_RESULT_KEY", "rule_optimizer"))
                # results[_ck] が既にある場合（月末更新済み）は
                # contraction_flag だけ上書きし、他のキー（weights等）は保持する
                _ro_existing = results.get(_ck, {})
                if isinstance(_ro_existing, dict):
                    _ro_existing["contraction_flag"]    = _contraction
                    _ro_existing["contraction_mode"]    = _contraction.get("mode", "Normal")
                    _ro_existing["contraction_detail"]  = _contraction.get("detail", {})
                    results[_ck] = _ro_existing
                else:
                    results[_ck] = {
                        "contraction_flag":   _contraction,
                        "contraction_mode":   _contraction.get("mode", "Normal"),
                        "contraction_detail": _contraction.get("detail", {}),
                    }

                _mode = _contraction.get("mode", "Normal")
                _cnt  = _contraction.get("triggered_count", 0)
                _coef = _contraction.get("coefficient", 1.00)
                _icon = {"Normal": "✅", "Warning": "⚠️", "Emergency": "🚨"}.get(_mode, "✅")
                print(
                    f"\n  {_icon} [縮退判定] モード={_mode} "
                    f"/ トリガー数={_cnt} "
                    f"/ 係数={_coef:.2f}"
                )
                if _contraction.get("triggered_flags"):
                    print(f"      発動条件: {_contraction['triggered_flags']}")

                # 縮退履歴を保存（Warning/Emergency 時のみ）
                if _mode != "Normal":
                    try:
                        _ro_cf.save_contraction_history(cfg, _contraction)
                    except Exception:
                        pass

                logger.add(
                    "contraction_flag",
                    "alert" if _mode == "Emergency"
                    else ("warning" if _mode == "Warning" else "success"),
                    f"縮退モード={_mode} / triggers={_cnt} / coeff={_coef:.2f}",
                )
            else:
                # rule_optimizer が未配置または関数未実装の場合はNormalで初期化
                results.setdefault("rule_optimizer", {})
                results["rule_optimizer"].setdefault("contraction_flag", {
                    "mode": "Normal", "triggered_count": 0,
                    "triggered_flags": [], "detail": {}, "coefficient": 1.00,
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                })
                results["rule_optimizer"].setdefault("contraction_mode", "Normal")
        except Exception as _rof_e:
            # 縮退判定失敗は signal_aggregator を止めない
            print(f"  ⚠️  縮退フラグ判定エラー（スキップ）: {str(_rof_e)[:80]}")
            results.setdefault("rule_optimizer", {})
            results["rule_optimizer"].setdefault("contraction_flag", {
                "mode": "Normal", "triggered_count": 0,
                "triggered_flags": [], "detail": {}, "coefficient": 1.00,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
            results["rule_optimizer"].setdefault("contraction_mode", "Normal")
                        # ── [WEEKLY] 学術カンファレンス追跡 ────────────────
    if getattr(cfg, "ENABLE_ACADEMIC_CONF_TRACKER", False) and _in_mode("signal_aggregator"):
        try:
            _act = _load_local_module(
                "academic_conf_tracker",
                "/content/academic_conf_tracker.py",
                cfg.DRIVE_BASE / "academic_conf_tracker.py",
            )
            if _act:
                act_result = _act.run_academic_conf_tracker(cfg, force_refresh=False)
                results.setdefault("wave7_trackers", {})["academic"] = act_result
                _n_confs = act_result.get("stats", {}).get("total_confs", 0)
                _n_papers = act_result.get("stats", {}).get("total_papers", 0)
                logger.add(
                    "academic_conf_tracker", "success",
                    f"学会={_n_confs} 論文={_n_papers}",
                )
        except Exception as e:
            logger.add("academic_conf_tracker", "error", str(e)[:100])
            print(f"  ⚠️  学術追跡エラー: {str(e)[:80]}")

        # ── [WEEKLY] 標準化動向追跡 ────────────────────────
    if getattr(cfg, "ENABLE_STANDARDIZATION_TRACKER", False) and _in_mode("signal_aggregator"):
        try:
            _ist = _load_local_module(
                "industry_standard_tracker",
                "/content/industry_standard_tracker.py",
                cfg.DRIVE_BASE / "industry_standard_tracker.py",
            )
            if _ist:
                ist_result = _ist.run_industry_standard_tracker(cfg, force_refresh=False)
                results.setdefault("wave7_trackers", {})["standardization"] = ist_result
                _n_std  = ist_result.get("stats", {}).get("total_standards_tracked", 0)
                _n_comp = ist_result.get("stats", {}).get("total_companies_tracked", 0)
                logger.add(
                    "industry_standard_tracker", "success",
                    f"標準={_n_std} 企業={_n_comp}",
                )
        except Exception as e:
            logger.add("industry_standard_tracker", "error", str(e)[:100])
            print(f"  ⚠️  標準化追跡エラー: {str(e)[:80]}")

        # ── signal_aggregator へ wave7_trackers を渡す ─────
        # signal_aggregator.py の抽出関数が results["wave7_trackers"] と
        # results["_cfg"] を参照するため、ここで results に注入する。
    results.setdefault("wave7_trackers", {})
    results["_cfg"] = cfg  # signal_aggregator の抽出関数内での cfg 参照用

        # ── taxonomy node_hotness ──────────────────
        # arxiv=先行 / news+policy=準先行を分離合成した技術テーマ加熱度。
        # signal_aggregator のカテゴリVが sector_scores を消費するため
        # アグリゲーターの直前で実行する。
    if getattr(cfg, "ENABLE_TAXONOMY_SIGNAL", False):
        try:
            _th = _load_local_module(
                "taxonomy_hotness",
                "/content/taxonomy_hotness.py",
                cfg.DRIVE_BASE / "taxonomy_hotness.py",
            )
            if _th:
                th_result = _th.run_taxonomy_hotness(cfg, results)
                results["taxonomy_signal"] = th_result
                if not th_result.get("skipped"):
                    logger.add("taxonomy_hotness", "success",
                               f"ノード:{th_result.get('nodes', 0)} "
                               f"業種:{len(th_result.get('sector_scores', {}))} "
                               f"先行のみ:{len(th_result.get('leading_only', []))}件")
            else:
                print("  ⚠️  taxonomy_hotness.py が見つかりません")
                logger.add("taxonomy_hotness", "warn", "taxonomy_hotness.py not found")
        except Exception as e:
            logger.add("taxonomy_hotness", "error", str(e)[:100])
            print(f"  ⚠️  taxonomy_hotnessエラー: {str(e)[:80]}")
    else:
        print("  ⏩ taxonomy_hotness: スキップ（ENABLE_TAXONOMY_SIGNAL=False）")

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
                    _contraction_mode = sa_result.get("contraction_mode", "Normal")
                    _def_count = len(sa_result.get("defensive_sectors", []))
                    logger.add("signal_aggregator", "success",
                               f"レジーム:{regime} / 買い推奨:{len(top)}業種 "
                               f"/ 縮退:{_contraction_mode} / 防御:{_def_count}業種")
                    # カテゴリ別スコアのパネル（月次のシグナル淘汰が20日先リターンと突き合わせる入力）。
                    # sector_signals はこのステージで初めてできるため、ここで毎日書く。
                    # alpha_attributer の呼び出し位置（週次の rule_optimizer・このステージより前）では書けない。
                    try:
                        _aa_panel = _load_local_module(
                            "alpha_attributer",
                            "/content/alpha_attributer.py",
                            cfg.DRIVE_BASE / "alpha_attributer.py",
                        )
                        if _aa_panel and getattr(cfg, "ENABLE_ALPHA_ATTRIBUTION", True):
                            _n_panel = _aa_panel.record_category_panel(cfg, results)
                            logger.add("category_panel", "success" if _n_panel else "skipped",
                                       f"カテゴリパネル: {_n_panel}業種" if _n_panel else "同日記録済み・内訳なし")
                    except Exception as _cpe:
                        logger.add("category_panel", "error", str(_cpe)[:100])
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
            _sagg = _load_local_module(
                "signal_aggregator",
                "/content/signal_aggregator.py",
                cfg.DRIVE_BASE / "signal_aggregator.py",
            )
            if _sagg and hasattr(_sagg, "calc_total_signal_score"):
                tss = _sagg.calc_total_signal_score(results, cfg)
                results["total_signal_score"] = tss
                print(f"\n  🧠 統合シグナルスコア: "
                      f"{tss['total_signal_score']:.0f}点 "
                      f"| {tss['signal_label']} "
                      f"| 推奨: {tss['recommended_action']}")
        except Exception as e:
            print(f"  ⚠️  統合スコア算出エラー: {e}")
        # ── JP/US共通composite（unified_scores） ──
        # 保有銘柄＋us_screener上位50へ共通形式のスコアを付与（SPEC_core §8-2）。
        # 既存スコアの adapter 写像のみ（再計算しない）。
    if getattr(cfg, "ENABLE_SCORE_CALCULATOR", True) and _in_mode("score_calculator"):
        try:
            _scu = _load_local_module(
                "score_calculator",
                "/content/score_calculator.py",
                cfg.DRIVE_BASE / "score_calculator.py",
            )
            if _scu:
                if getattr(cfg, "ENABLE_REAL_ESTATE_SIGNAL", True):
                    try:
                        import stock_real_estate
                        _sre = stock_real_estate.run(cfg, results)
                        logger.add("stock_real_estate", "success" if _sre.get("count") else "warn",
                                   f"不動産ボーナス{_sre.get('count', 0)}銘柄（非0 {_sre.get('nonzero', 0)}）"
                                   f" / 業種マスタ{_sre.get('sector_master_rows', 0)}行")
                    except Exception as e:
                        logger.add("stock_real_estate", "error", str(e)[:100])
                        print(f"  ⚠️  銘柄単位の不動産ボーナス失敗: {str(e)[:80]}")
                _us_result = _scu.run_score_calculator(cfg, results)
                results["unified_scores"] = _us_result
                if _us_result.get("skipped"):
                    logger.add("score_calculator", "warn", "対象ユニバースなし（skip）")
                else:
                    logger.add("score_calculator", "success",
                               f"共通スコア{_us_result['covered']}銘柄 "
                               f"/ 未算出{len(_us_result.get('missing', []))}銘柄")
            else:
                print("  ⚠️  score_calculator.py が見つかりません")
        except Exception as e:
            logger.add("score_calculator", "error", str(e)[:100])
            print(f"  ⚠️  共通スコア計算エラー: {str(e)[:80]}")
    # 投資審議の DB キュー。クラウドの定時審議はこのキューだけを読むので、失敗は error で残す。
    if getattr(cfg, "ENABLE_DELIBERATION_QUEUE", True) and _in_mode("deliberation_queue"):
        try:
            import deliberation_queue as _dlq
            _dq_res = _dlq.run_daily(cfg, (results.get("screener") or {}).get("candidates") or [])
            results["deliberation_queue"] = _dq_res
            _pf = _dq_res.get("packet_failed") or {}
            # 承認の反映と審議結果の書き出し（vault・board 用ミラー）。失敗してもキュー作成の結果は変えず warn にする
            _ex = _dlq.run_exports(cfg)
            results["deliberation_queue"]["exports"] = _ex
            _ex_err = _ex.get("errors") or {}
            logger.add("deliberation_queue", "warn" if (_pf or _ex_err) else "success",
                       f"キュー{_dq_res.get('queued', 0)}件 / パケット{_dq_res.get('packets', 0)}件"
                       + (f" / パケット失敗{len(_pf)}: {', '.join(list(_pf)[:5])}" if _pf else "")
                       + (f" / 書き出し失敗: {_ex_err}" if _ex_err else
                          f" / 承認反映{(_ex.get('apply_approvals') or {}).get('applied', 0)}件"
                          f"・書き出し{(_ex.get('export_results') or {}).get('exported', 0)}件"))
        except Exception as e:
            logger.add("deliberation_queue", "error", str(e)[:150])
            print(f"  ⚠️  審議キューの作成失敗: {str(e)[:100]}")
        # ── MLメタモデル: 特徴量スナップショット蓄積 ──
        # signal_aggregator の23カテゴリ components を日次で features.jsonl へ追記。
        # 学習は Colab（ml/train_meta.ipynb・週次手動）。ここは蓄積のみで軽量。
    if getattr(cfg, "ENABLE_ML_META_DATASET", True) and _in_mode("ml_meta"):
        try:
            _mlds = _load_local_module(
                "dataset_builder",
                "/content/ml/dataset_builder.py",
                cfg.DRIVE_BASE / "ml" / "dataset_builder.py",
            )
            if _mlds:
                _snap = _mlds.snapshot(cfg, results)
                results["ml_meta_dataset"] = _snap
                if not _snap.get("skipped"):
                    logger.add("ml_meta_dataset", "success",
                               f"{_snap['rows']}行追記（累計{_snap.get('total_rows', 0)}行）")
                else:
                    logger.add(
                        "ml_meta_dataset", "warn",
                        f"特徴量行なし（skip）: "
                        f"{_snap.get('reason', '理由不明')}"[:100])
            else:
                print("  ⚠️  ml/dataset_builder.py が見つかりません")
                                # ロード失敗も台帳に残す（print だけだと「記録なし」に見える）。
                logger.add("ml_meta_dataset", "warn",
                           "ml/dataset_builder.py not found")
        except Exception as e:
            logger.add("ml_meta_dataset", "error", str(e)[:100])
            print(f"  ⚠️  ML特徴量スナップショットエラー: {str(e)[:80]}")
        # ── MLメタモデル: 補正テーブル適用 ──────────
        # ML_META_ENABLED=True（昇格基準クリア＋利用者承認）の場合のみ。
        # 昇格基準（IC>=0.02・t>=2.0）はテーブル読込時にも再検証される。
    if getattr(cfg, "ML_META_ENABLED", False) and _in_mode("ml_meta"):
        try:
            _mlca = _load_local_module(
                "corrector_applier",
                "/content/ml/corrector_applier.py",
                cfg.DRIVE_BASE / "ml" / "corrector_applier.py",
            )
            if _mlca:
                _corr = _mlca.run_ml_meta_corrector(cfg, results)
                results["ml_meta_correction"] = _corr
                if _corr.get("skipped"):
                    logger.add("ml_meta_correction", "warn", _corr.get("reason", ""))
                else:
                    logger.add("ml_meta_correction", "success",
                               f"{_corr['applied']}銘柄補正 "
                               f"(IC={_corr.get('table_ic')})")
            else:
                print("  ⚠️  ml/corrector_applier.py が見つかりません")
        except Exception as e:
            logger.add("ml_meta_correction", "error", str(e)[:100])
            print(f"  ⚠️  ML補正適用エラー: {str(e)[:80]}")
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
    
# ── [I-1] 求人票モニター（週次・廃止済み）──────────────
    # 実行条件: ENABLE_JOB_POSTING_MONITOR=True かつ weeklyモードで月曜実行
    if getattr(cfg, "ENABLE_JOB_POSTING_MONITOR", True) and _in_mode("job_posting"):
        try:
            _jpm = _load_local_module(
                "job_posting_monitor",
                "/content/job_posting_monitor.py",
                cfg.DRIVE_BASE / "job_posting_monitor.py",
            )
            if _jpm:
                jpm_result = _jpm.run_job_posting_monitor(cfg, results)
                results["job_posting"] = jpm_result
                if not jpm_result.get("skipped"):
                    surge     = jpm_result.get("surge_detected", [])
                    boosts    = jpm_result.get("sector_boosts",  {})
                    total_p   = jpm_result.get("total_postings", 0)
                    cached    = jpm_result.get("cached", False)
                    logger.add(
                        "job_posting_monitor", "success",
                        f"取得{total_p}件 / 急増{len(surge)}カテゴリ"
                        f"{surge[:3]} / boost={boosts}"
                        f"{' [キャッシュ]' if cached else ''}",
                    )
                else:
                    logger.add("job_posting_monitor", "skipped", "モニタースキップ")
            else:
                print("  ⚠️  job_posting_monitor.py が見つかりません")
        except Exception as _jpme:
            logger.add("job_posting_monitor", "error", str(_jpme)[:100])
            print(f"  ⚠️  求人票モニターエラー（スキップ）: {str(_jpme)[:80]}")
    else:
        print("  ⏩ 求人票モニター: スキップ（週次モード外 or フラグOFF）")

        # ── [I-3] 役員転職トラッカー（週次・停止中）──────────
        # 実行条件: ENABLE_EXECUTIVE_TRANSITION=True かつ weeklyモードで月曜実行
    if getattr(cfg, "ENABLE_EXECUTIVE_TRANSITION", True) and _in_mode("executive_transition"):
        try:
            _ett = _load_local_module(
                "executive_transition_tracker",
                "/content/executive_transition_tracker.py",
                cfg.DRIVE_BASE / "executive_transition_tracker.py",
            )
            if _ett:
                ett_result = _ett.run_executive_transition_tracker(cfg, results)
                results["executive_transition"] = ett_result
                if not ett_result.get("skipped"):
                    alerts  = ett_result.get("transition_alerts", [])
                    boosts  = ett_result.get("sector_boosts", {})
                    total_a = ett_result.get("total_analyzed", 0)
                    cached  = ett_result.get("cached", False)
                    logger.add(
                        "executive_transition_tracker", "success",
                        f"分析{total_a}社 / 転換{len(alerts)}社"
                        f"{[a['ticker'] for a in alerts[:3]]} / boost={boosts}"
                        f"{' [キャッシュ]' if cached else ''}",
                    )
                else:
                    logger.add(
                        "executive_transition_tracker", "skipped", "モニタースキップ"
                    )
            else:
                print("  ⚠️  executive_transition_tracker.py が見つかりません")
        except Exception as _ette:
            logger.add("executive_transition_tracker", "error", str(_ette)[:100])
            print(f"  ⚠️  役員転職トラッカーエラー（スキップ）: {str(_ette)[:80]}")
    else:
        print("  ⏩ 役員転職トラッカー: スキップ（週次モード外 or フラグOFF）")

        # ── [I-4] 施設着工モニター（週次・廃止済み）────────────
        # 実行条件: ENABLE_FACILITY_EXPANSION=True かつ weeklyモードで月曜実行
    if getattr(cfg, "ENABLE_FACILITY_EXPANSION", True) and _in_mode("facility_expansion"):
        try:
            _fem = _load_local_module(
                "facility_expansion_monitor",
                "/content/facility_expansion_monitor.py",
                cfg.DRIVE_BASE / "facility_expansion_monitor.py",
            )
            if _fem:
                fem_result = _fem.run_facility_expansion_monitor(cfg, results)
                results["facility_expansion"] = fem_result
                if not fem_result.get("skipped"):
                    signals  = fem_result.get("signal_projects", [])
                    boosts   = fem_result.get("sector_boosts", {})
                    total_f  = fem_result.get("total_fetched", 0)
                    cached   = fem_result.get("cached", False)
                    logger.add(
                        "facility_expansion_monitor", "success",
                        f"取得{total_f}件 / シグナル{len(signals)}件"
                        f" / boost={boosts}"
                        f"{' [キャッシュ]' if cached else ''}",
                    )
                else:
                    logger.add(
                        "facility_expansion_monitor", "skipped", "モニタースキップ"
                    )
            else:
                print("  ⚠️  facility_expansion_monitor.py が見つかりません")
        except Exception as _feme:
            logger.add("facility_expansion_monitor", "error", str(_feme)[:100])
            print(f"  ⚠️  施設着工モニターエラー（スキップ）: {str(_feme)[:80]}")
    else:
        print("  ⏩ 施設着工モニター: スキップ（週次モード外 or フラグOFF）")

    # ── [EVT-1] イベントホライズン（事前アラート・日次）──────────
    if (getattr(cfg, "ENABLE_EVENT_HORIZON", True)
            and _in_mode("event_horizon")):
        try:
            _evh = _load_local_module(
                "event_horizon_scanner",
                "/content/event_horizon_scanner.py",
                cfg.DRIVE_BASE / "event_horizon_scanner.py",
            )
            if _evh:
                evh_result = _evh.run_event_horizon_scanner(cfg, results)
                results["event_horizon"] = evh_result
                logger.add(
                    "event_horizon", "success",
                    f"{evh_result.get('alert_days',5)}日以内 "
                    f"{len(evh_result.get('upcoming',[]))}件 "
                    f"/ アラート{len(evh_result.get('alerts',[]))}件",
                )
        except Exception as _evhe:
            logger.add("event_horizon", "error", str(_evhe)[:100])
            print(f"  ⚠️  イベントホライズンエラー（スキップ）: {str(_evhe)[:60]}")
    else:
        print("  ⏩ イベントホライズン: スキップ")

    # ── [OPT-1] オプション戦略アドバイザー（日次）─────────────────
    # event_horizon の後に実行（満期前イベント跨ぎ判定に使うため）。
    if (getattr(cfg, "ENABLE_OPTIONS_ADVISOR", True)
            and _in_mode("options_advisor")):
        try:
            _opa = _load_local_module(
                "options_strategy_advisor",
                "/content/options_strategy_advisor.py",
                cfg.DRIVE_BASE / "options_strategy_advisor.py",
            )
            if _opa:
                opa_result = _opa.run_options_advisor(cfg, results)
                results["options_advisor"] = opa_result
                logger.add(
                    "options_advisor", "success",
                    f"ユニバース{len(opa_result.get('universe',[]))} "
                    f"/ 提案{len(opa_result.get('suggestions',[]))}件",
                )
        except Exception as _opae:
            logger.add("options_advisor", "error", str(_opae)[:100])
            print(f"  ⚠️  オプションアドバイザーエラー（スキップ）: {str(_opae)[:60]}")
        # S4 のオプションの仮想口座（提案を理論価格で仮想に建て、毎日評価・満期で清算。証券会社の口座は使わない）
        if getattr(cfg, "ENABLE_PAPER_OPTIONS", True) and results.get("options_advisor"):
            try:
                from paper import options_book as _ob
                results["paper_options"] = _ob.run(cfg, results)
                logger.add("paper_options", "skipped" if results["paper_options"].get("skipped") else "success",
                           str(results["paper_options"])[:150])
            except Exception as _obe:
                logger.add("paper_options", "error", str(_obe)[:100])
    else:
        print("  ⏩ オプションアドバイザー: スキップ")

        # ── [ONT-1] オントロジー層エクスポート（週次）──
        # エンティティグラフ（企業・シグナル・テーマ・意思決定）を BQ へ追記。
        # BQ認証なし環境では JSONL ローカル出力に自動フォールバックする。
    if (getattr(cfg, "ENABLE_ONTOLOGY_EXPORT", True)
            and _in_mode("ontology_export")):
        try:
            _oe = _load_local_module(
                "ontology_exporter",
                "/content/ontology_exporter.py",
                cfg.DRIVE_BASE / "ontology_exporter.py",
            )
            if _oe:
                ont_result = _oe.run_ontology_exporter(cfg, results)
                results["ontology_export"] = ont_result
                _st = ont_result.get("stats", {})
                logger.add(
                    "ontology_export",
                    "success" if ont_result.get("success") else "error",
                    f"mode={ont_result.get('mode','')} "
                    f"/ 企業{_st.get('company_entity',0)} "
                    f"/ イベント{_st.get('signal_event',0)} "
                    f"/ 意思決定{_st.get('decision_event',0)}",
                )
        except Exception as _oee:
            logger.add("ontology_export", "error", str(_oee)[:100])
            print(f"  ⚠️  オントロジー出力エラー（スキップ）: {str(_oee)[:60]}")
    else:
        print("  ⏩ オントロジー出力: スキップ（週次モード外 or フラグOFF）")

        # ── [Step F] 週次投資判断レポート（週次の全モジュール完了後に生成）──
        # signal_aggregator / inference / ontology_export / position_sizer 等の結果を読むので末尾に置く
        # （前に置くとそれらのセクションが「データなし」になる）。
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
    

        # ── [PRICE-1] 株価日足をBQへ永続化（全モジュールの取得完了後）────
        # yf_cache が実行中に集めた日足をまとめて書き出す（追加取得はしない）。必ず取得側より後に置くこと。
    if getattr(cfg, "ENABLE_PRICE_HISTORY_WRITER", True) and _in_mode("screener"):
        try:
            _phw = _load_local_module(
                "price_history_writer",
                "/content/price_history_writer.py",
                cfg.DRIVE_BASE / "price_history_writer.py",
            )
            if _phw:
                _price_res = _phw.run_price_history_writer(cfg, results)
                results["price_history"] = _price_res
                if _price_res.get("skipped"):
                    logger.add("price_history", "skipped",
                               str(_price_res.get("reason", ""))[:80])
                else:
                    logger.add(
                        "price_history",
                        "success" if _price_res.get("inserted") else "warn",
                        f"{_price_res.get('tickers', 0)}銘柄 / "
                        f"{_price_res.get('rows', 0)}行 / "
                        f"BQ={_price_res.get('inserted', False)}",
                    )
        except Exception as _phwe:
            logger.add("price_history", "error", str(_phwe)[:100])
            print(f"  ⚠️  株価履歴格納エラー（スキップ）: {str(_phwe)[:60]}")

    # ── [PIPE-2] results スキーマ契約チェック（警告のみ） ──────
    try:
        _rs = _load_local_module(
            "results_schema",
            "/content/results_schema.py",
            cfg.DRIVE_BASE / "results_schema.py",
        )
        if _rs:
            results["_schema_check"] = _rs.validate_results(results)
    except Exception as _rse:
        print(f"  ⚠️  スキーマチェックエラー（スキップ）: {str(_rse)[:60]}")

    # ── [OBS-1] モジュール実行台帳の確定 ──────────────────────
    # RunLogger のフラットログを日次台帳に集約し、連続失敗・配線漏れを
    # 可視化する。全モジュール実行後（return 直前）に必ず実行する。
    if getattr(cfg, "ENABLE_MODULE_LEDGER", True):
        try:
            _ml = _load_local_module(
                "module_ledger",
                "/content/module_ledger.py",
                cfg.DRIVE_BASE / "module_ledger.py",
            )
            if _ml:
                results["module_ledger"] = _ml.finalize_daily_ledger(
                    cfg, logger._logs
                )
        except Exception as _mle:
            print(f"  ⚠️  実行台帳エラー（スキップ）: {str(_mle)[:60]}")

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
        import streamlit as st # type: ignore
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
                    try:
                        metric_val = f"{float(v):.3g}" if v is not None else "N/A"
                    except (TypeError, ValueError):
                        metric_val = str(v)[:15] if v is not None else "N/A"
                    st.metric(label, metric_val)

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
                                    from base_agent import fmt_cap
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

    print("  ⏰ スケジューラー起動:")
    print(f"     デイリー  : 毎日 {run_time} [daily モード]")
    print(f"     ウィークリー: 毎週{weekly_label} {run_time} [weekly モード]")
    print("  ⚠️  Colab セッション中のみ有効")
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
