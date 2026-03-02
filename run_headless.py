# ==============================================================
# run_headless.py  v1.0  (5-C)
# GitHub Actions / ローカル ヘッドレス実行スクリプト
# ==============================================================
# 【役割】
#   Streamlit UI を起動せず、run_all_tasks() だけを実行する。
#   GitHub Actions の daily_run.yml からこのスクリプトを呼び出す。
#
# 【環境変数によるパス切り替え】
#   STOCK_ANALYZER_BASE  : データ保存ルートパス（省略時はスクリプトと同階層）
#   STOCK_ANALYZER_MODE  : "daily" | "weekly" | "all"（省略時は "daily"）
#
# 【GitHub Actions での使い方】
#   python run_headless.py
#   python run_headless.py --mode weekly
#   python run_headless.py --mode all
#
# 【必要な GitHub Secrets】（Settings > Secrets and variables > Actions）
#   ALERT_GMAIL_FROM      : 送信元 Gmail アドレス
#   ALERT_GMAIL_PASSWORD  : Gmail App Password（16桁）
#   ALERT_GMAIL_TO        : 送信先アドレス
#   ALERT_LINE_TOKEN      : LINE Messaging API チャネルアクセストークン（任意）
#   ALERT_LINE_USER_ID    : LINE ユーザーID（任意）
#   EDINET_API_KEY        : EDINET API キー（任意）
#   POLICY_FRED_API_KEY   : FRED API キー（任意）
#
# 【出力】
#   BASE_DIR/results/    : セクター分析結果 JSON
#   BASE_DIR/insights/   : Markdown レポート
#   BASE_DIR/logs/       : 実行ログ
#   BASE_DIR/data/       : キャッシュ・JSONデータ
# ==============================================================

import argparse
import importlib.util
import os
import sys
from datetime import datetime
from pathlib import Path


def _load_module(name: str, path: Path):
    """指定パスのモジュールを動的ロードする"""
    if not path.exists():
        print(f"  ⚠️  {name}: {path} が見つかりません")
        return None
    spec   = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def apply_env_overrides(cfg):
    """
    環境変数から config の設定値を上書きする。
    GitHub Actions の Secrets → env に対応。

    適用順: config.py のデフォルト値 → 環境変数
    """
    ENV_MAP = {
        # アラート通知
        "ALERT_GMAIL_FROM":      "ALERT_GMAIL_FROM",
        "ALERT_GMAIL_PASSWORD":  "ALERT_GMAIL_PASSWORD",
        "ALERT_GMAIL_TO":        "ALERT_GMAIL_TO",
        "ALERT_LINE_TOKEN":      "ALERT_LINE_TOKEN",
        "ALERT_LINE_USER_ID":    "ALERT_LINE_USER_ID",
        # API キー
        "EDINET_API_KEY":        "EDINET_API_KEY",
        "POLICY_FRED_API_KEY":   "POLICY_FRED_API_KEY",
    }
    overridden = []
    for attr, env_key in ENV_MAP.items():
        val = os.environ.get(env_key, "")
        if val:
            setattr(cfg, attr, val)
            overridden.append(attr)

    # DRIVE_BASE をローカルパスに切り替え
    base_env = os.environ.get("STOCK_ANALYZER_BASE", "")
    if base_env:
        base = Path(base_env)
    else:
        base = Path(__file__).parent / "stock_data"

    # パスを再設定（config.py の DRIVE_BASE 相当）
    cfg.DRIVE_BASE  = base
    cfg.DATA_DIR    = base / "data"
    cfg.RESULT_DIR  = base / "results"
    cfg.INSIGHT_DIR = base / "insights"
    cfg.ARXIV_DIR   = base / "insights" / "arxiv"
    cfg.SECTOR_DIR  = base / "insights" / "sectors"
    cfg.MACRO_DIR   = base / "insights" / "macro"
    cfg.LOG_DIR     = base / "logs"
    cfg.AGENTS_DIR  = Path(__file__).parent / "sector_agents"

    if overridden:
        print(f"  🔑 環境変数を適用: {', '.join(overridden)}")
    print(f"  📁 BASE_DIR: {base}")
    return cfg


def main():
    parser = argparse.ArgumentParser(
        description="Stock Analyzer ヘッドレス実行"
    )
    parser.add_argument(
        "--mode",
        choices=["daily", "weekly", "all"],
        default=os.environ.get("STOCK_ANALYZER_MODE", "daily"),
        help="実行モード（デフォルト: daily）",
    )
    args = parser.parse_args()

    print(f"\n{'='*60}")
    print(f"  Stock Analyzer ヘッドレス実行")
    print(f"  モード: {args.mode.upper()}")
    print(f"  開始: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    # config.py を読み込む
    here = Path(__file__).parent
    cfg_path = here / "config.py"
    if not cfg_path.exists():
        print(f"ERROR: config.py が見つかりません: {cfg_path}")
        sys.exit(1)

    cfg_mod = _load_module("config", cfg_path)
    if cfg_mod is None:
        sys.exit(1)

    # 環境変数で設定を上書き
    cfg_mod = apply_env_overrides(cfg_mod)

    # ディレクトリ作成
    for d in [cfg_mod.DATA_DIR, cfg_mod.RESULT_DIR, cfg_mod.INSIGHT_DIR,
              cfg_mod.ARXIV_DIR, cfg_mod.SECTOR_DIR, cfg_mod.MACRO_DIR,
              cfg_mod.LOG_DIR,
              cfg_mod.INSIGHT_DIR / "ideas",
              cfg_mod.INSIGHT_DIR / "macro",]:
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    # stock_analyzer_main.py を読み込む
    main_path = here / "stock_analyzer_main.py"
    if not main_path.exists():
        print(f"ERROR: stock_analyzer_main.py が見つかりません: {main_path}")
        sys.exit(1)

    # sys.modules に config を登録（main.py の _load_config() が参照する）
    sys.modules["config"] = cfg_mod

    main_mod = _load_module("stock_analyzer_main", main_path)
    if main_mod is None:
        sys.exit(1)

    # main_mod.cfg を環境変数適用済みのものに差し替え
    main_mod.cfg = cfg_mod

    # 実行
    print(f"\n  ▶ run_all_tasks(mode='{args.mode}') 開始...")
    try:
        results = main_mod.run_all_tasks(mode=args.mode)
        n_sectors = len(results.get("sectors", {}))
        n_alerts  = results.get("alerts", {}).get("alerts_sent", 0)
        print(f"\n  ✅ 完了: sectors={n_sectors} alerts={n_alerts}")
        sys.exit(0)
    except Exception as e:
        print(f"\n  ❌ 実行エラー: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
