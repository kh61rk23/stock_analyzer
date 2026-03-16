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
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
logging.getLogger("streamlit.runtime.caching.cache_data_api").setLevel(logging.ERROR)
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


def evaluate_macro_signals(snapshot: dict) -> list:
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
            })

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

# ==============================================================
# BLOCK X: スコア履歴・スクリーナー履歴 蓄積
# ==============================================================
def _append_screener_history(cfg, screener_result: dict) -> None:
    """
    screener通過銘柄を日次で蓄積する。
    保存先: DATA_DIR/screener_history.json
    形式:   {"YYYY-MM-DD": [{"ticker": "8306.T", "sector": "1028",
                             "composite_score": 72.0, "per": 12.3, ...}]}
    保持:   直近365日分（バックテストに使うため長め）
    用途:   「N日前にscreenerを通過した銘柄のその後のリターン」検証
    """
    import json
    try:
        path = Path(cfg.DATA_DIR) / "screener_history.json"
        today = datetime.now().strftime("%Y-%m-%d")

        # 既存データ読み込み
        history = {}
        if path.exists():
            try:
                history = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                history = {}

        # 通過銘柄リストを抽出
        candidates = screener_result.get("candidates", [])
        passed = [
            {
                "ticker":          c.get("ticker", ""),
                "sector_code":     c.get("sector_code", ""),
                "composite_score": c.get("composite_score"),
                "per":             c.get("per"),
                "pbr":             c.get("pbr"),
                "roe":             c.get("roe"),
                "market_cap":      c.get("market_cap"),
            }
            for c in candidates
            if c.get("pass_filters")
        ]

        if passed:
            history[today] = passed

        # 365日分だけ保持
        keys = sorted(history.keys())
        if len(keys) > 365:
            for old_key in keys[:-365]:
                del history[old_key]

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(history, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        print(f"  ✅ screener_history: {len(passed)}銘柄を記録（累計{len(history)}日分）")
    except Exception as e:
        print(f"  ⚠️  screener_history 保存失敗: {str(e)[:60]}")

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
    """arxiv_monitor.py を実行する"""
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
        return False

    try:
        results  = mod.run_monitor()
        n        = results.get("investment_ideas_found", 0)
        n_papers = results.get("new_papers", 0)
        logger.add("arxiv_monitor", "success",
                   f"新着{n_papers}件, 候補{n}件")
        return True
    except Exception as e:
        msg = str(e)[:120]
        print(f"  ❌ ArXiv 監視エラー: {msg}")
        logger.add("arxiv_monitor", "error", msg)
        return False


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
        snapshot = fetch_macro_snapshot()
        signals  = evaluate_macro_signals(snapshot)
        _save_macro_report(snapshot, signals)
        results["macro"] = True
    else:
        print("  ⏩ マクロ分析: スキップ（config.ENABLE_MACRO=False / Step 2 で有効化）")

    # ── セクター分析（2-A 追加）─────────────────────────────────
    if cfg.ENABLE_SECTOR and cfg.IMPLEMENTED_AGENTS and _in_mode("sector"):
        print(f"\n  🏭 セクター分析を実行中（{len(cfg.IMPLEMENTED_AGENTS)} 業種）...")
        macro_snapshot_for_sector = results.get("macro_snapshot", {})
        sector_results = {}
        for code in cfg.IMPLEMENTED_AGENTS:
            try:
                res = run_sector_analysis(code, macro_snapshot_for_sector)
                sector_results[code] = res
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
            _score_hist_path = Path("stock_data/data/score_history.json")
            if _score_hist_path.exists():
                import json as _json
                _score_hist_all = _json.loads(_score_hist_path.read_text())
            else:
                _score_hist_all = []
            
            import sys as _sys, importlib as _imp
            _sys.path.insert(0, str(Path(__file__).parent))
            _se = _imp.import_module("score_explainer")
            _snapshot = results.get("macro_snapshot", {})
            
            for _code, _res in sector_results.items():
                # score_history から該当業種の系列を抽出
                _hist = [
                    {"date": h["date"], "composite_score": h.get("scores", {}).get(_code)}
                    for h in _score_hist_all
                    if isinstance(h.get("scores"), dict) and h["scores"].get(_code) is not None
                ]
                _res["factor_breakdown"] = _se.factor_breakdown(
                    result         = _res,
                    score_history  = _hist,
                    macro_snapshot = _snapshot,
                )
            print("  ✅ factor_breakdown 付与完了")
        except Exception as _e:
            print(f"  ⚠️  factor_breakdown 付与エラー: {_e}")
        
        print(f"  ✅ セクター分析完了: {len(sector_results)} 業種")

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
    if getattr(cfg, "ENABLE_MULTI_CURRENCY", True) and _in_mode("policy"):
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
                for _code, _delta in mc_adj.items():
                    if _code in _sr and isinstance(_sr[_code], dict) and _delta != 0:
                        _sr[_code]["composite_score"] = round(
                            max(-30, min(100,
                                _sr[_code].get("composite_score", 0) + _delta
                            )), 1
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
    # ── [F-1] イールドカーブ → セクタースコア反映 ───────────────────
    _bc_result = results.get("bond_commodity", {})
    _sector_res_for_f1 = results.get("sectors", {})
    if _bc_result and _sector_res_for_f1:
        try:
            import sys as _sys, importlib as _imp
            _sys.path.insert(0, str(Path(__file__).parent))
            _bc_mod = _imp.import_module("bond_commodity_pipeline")
            _bc_mod.apply_recession_score_to_sectors(
                recession_score = _bc_result.get("recession_score", 0),
                curve_analysis  = _bc_result.get("curve_analysis", {}),
                sector_results  = _sector_res_for_f1,
            )
            results["sectors"] = _sector_res_for_f1
        except Exception as _e:
            print(f"  ⚠️  [F-1] リセッション補正エラー（スキップ）: {_e}")
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
                screen_result = _sc.run_stock_screener(cfg, sector_res)
                results["screener"] = screen_result
                logger.add("stock_screener", "success",
                           f"通過:{screen_result.get('pass_count',0)}銘柄")
            else:
                print("  ⚠️  stock_screener.py が見つかりません")
        except Exception as e:
            logger.add("stock_screener", "error", str(e)[:100])
            # [2-3] screener_history.json に追記
            try:
                import sys as _sys, importlib as _imp
                _sys.path.insert(0, str(Path(__file__).parent))
                _pt_mod = _imp.import_module("performance_tracker")
                _pt_mod.append_screener_history(cfg, screen_result)
            except Exception as _e:
                print(f"  ⚠️  screener_history 追記失敗（スキップ）: {_e}")
            print(f"  ⚠️  スクリーニングエラー: {str(e)[:80]}")
             # [A-7] 決算サプライズ分析（screener通過銘柄を対象に実行）
            try:
                _es = _load_local_module(
                    "earnings_surprise",
                    "/content/earnings_surprise.py",
                    cfg.DRIVE_BASE / "earnings_surprise.py",
                )
                if _es and getattr(cfg, "ENABLE_EARNINGS_SURPRISE", True):
                    _es_tickers = [
                        c["ticker"] for c in screen_result.get("candidates", [])
                        if c.get("pass_filters")
                    ]
                    if _es_tickers:
                        es_result = _es.run_earnings_surprise(cfg, _es_tickers)
                        results["earnings_surprise"] = es_result
                        logger.add("earnings_surprise", "success",
                                    f"ポジ{es_result.get('hit_pos',0)}社/"
                                    f"ネガ{es_result.get('hit_neg',0)}社")
            except Exception as _e:
                print(f"  ⚠️  earnings_surprise エラー（スキップ）: {_e}")
    else:
        if not getattr(cfg, "ENABLE_SCREENER", False):
            print("  ⏩ 個別株スクリーニング: スキップ（ENABLE_SCREENER=False）")

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
    
    elapsed = (datetime.now() - start).seconds
    print(f"\n{'='*60}")
    print(f"  ✅ 全タスク完了（{elapsed}秒）")
    print(f"  📁 結果: {cfg.RESULT_DIR}")
    print(f"  📝 知見: {cfg.INSIGHT_DIR}")
    print(f"{'='*60}")
    logger.tail(n=5)

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
                erm_result = _erm.run_edinet_rotation(cfg, results)
                results["edinet_rotation"] = erm_result
                logger.add("edinet_rotation", "success",
                           f"取得:{erm_result.get('ok_count',0)}社 "
                           f"カバレッジ:{erm_result.get('db_coverage',0):.1f}%")
        except Exception as e:
            logger.add("edinet_rotation", "error", str(e)[:100])
    
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
    
    # ── スコア履歴・スクリーナー履歴の蓄積 ────────────────
        try:
            if results.get("sectors"):
                _append_score_history(cfg, results["sectors"])
            if results.get("screener"):
                _append_screener_history(cfg, results["screener"])
        except Exception as e:
            print(f"  ⚠️  履歴蓄積エラー（スキップ）: {str(e)[:60]}")
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
