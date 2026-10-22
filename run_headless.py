# ==============================================================
# run_headless.py
# GitHub Actions から日次本体（stock_analyzer_main.run_all_tasks）を動かす入口。
# GitHub Secrets（環境変数）を config に写してから実行する。
# 保存ファイルの置き場は BigQuery（state_store.py・report_store.py）で、Drive には送らない。
# ==============================================================

import argparse
import importlib.util
import os
import sys
from datetime import datetime
from pathlib import Path

#指定されたパスにあるPythonファイルを、指定した名前のモジュールとして動的に読み込む関数
def _load_module(name: str, path: Path):
    if not path.exists():
        print(f"  WARNING: {name}: {path} が見つかりません")
        return None
    spec   = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

#github action secreatsの読み込み
def apply_env_overrides(cfg):
    ENV_MAP = {
        "ALERT_GMAIL_FROM":           "ALERT_GMAIL_FROM",
        "ALERT_GMAIL_PASSWORD":       "ALERT_GMAIL_PASSWORD",
        "ALERT_GMAIL_TO":             "ALERT_GMAIL_TO",
        "ALERT_LINE_TOKEN":           "ALERT_LINE_TOKEN",
        "ALERT_LINE_USER_ID":         "ALERT_LINE_USER_ID",
        "EDINETDB_API_KEY":           "EDINETDB_API_KEY",
        "EDINET_GOV_API_KEY":         "EDINET_GOV_API_KEY",
        "FUDOSAN_API_KEY":            "FUDOSAN_API_KEY",
        "SEISAKUDB_API_KEY":          "SEISAKUDB_API_KEY",
        "POLICY_FRED_API_KEY":        "POLICY_FRED_API_KEY",
        "ANTHROPIC_API_KEY":          "ANTHROPIC_API_KEY",
        "SEC_USER_AGENT":             "SEC_USER_AGENT",
        "GOOGLE_CSE_API_KEY":         "GOOGLE_CSE_API_KEY",
        "GOOGLE_CSE_ID":              "GOOGLE_CSE_ID",
        "DEEPL_API_KEY":              "DEEPL_API_KEY",
        "ESTAT_API_KEY":              "ESTAT_API_KEY",
        "NOTION_API_KEY":             "NOTION_API_KEY",
        "CONGRESS_API_KEY":           "CONGRESS_API_KEY",  # congress_monitor 用
    }
    overridden = []
    for attr, env_key in ENV_MAP.items():
        val = os.environ.get(env_key, "")
        if val:
            setattr(cfg, attr, val)
            overridden.append(attr)

    base_env = os.environ.get("STOCK_ANALYZER_BASE", "")
    base = Path(base_env) if base_env else Path(__file__).parent / "stock_data"

    cfg.DRIVE_BASE  = base
    cfg.DATA_DIR    = base / "data"
    cfg.RESULT_DIR  = base / "results"
    cfg.INSIGHT_DIR = base / "insights"
    cfg.ARXIV_DIR   = base / "insights" / "arxiv"
    cfg.SECTOR_DIR  = base / "insights" / "sectors"
    cfg.MACRO_DIR   = base / "insights" / "macro"
    cfg.LOG_DIR     = base / "logs"
    # DRIVE_BASE 由来のパスもここで揃える。
    #   特に CACHE_DIR は score_prior / policy_radar 等 10+ モジュールが参照し、
    #   取りこぼすと Colab パス /content に書こうとして Permission denied になる
    #   （score_prior が全業種無効化する）。
    #   config.py も STOCK_ANALYZER_BASE を見るが、env 未設定で
    #   base が __file__/stock_data に決まるローカル実行でも整合させるため明示する。
    cfg.CACHE_DIR             = base / "cache"
    cfg.SUBTHEME_JSON_PATH_JP = base / "data" / "jp_sector_subtheme.json"
    cfg.SUBTHEME_JSON_PATH_US = base / "data" / "us_sector_subtheme.json"

    if overridden:
        print(f"  KEY 環境変数を適用: {', '.join(overridden)}")
    print(f"  DIR BASE_DIR: {base}")
    return cfg

# 環境設定・分析実行・結果のクラウド保存までを全自動で一気通貫に行う、システムの実行制御（メインルーチン）関数
def main():
    parser = argparse.ArgumentParser(description="Stock Analyzer ヘッドレス実行")
    parser.add_argument(
        "--mode",
        choices=["daily", "weekly", "all"],
        default=os.environ.get("STOCK_ANALYZER_MODE", "daily"),
    )
    args = parser.parse_args()
    
    print(f"\n{'='*60}")
    print("  Stock Analyzer ヘッドレス実行")
    print(f"  モード: {args.mode.upper()}")
    print(f"  開始: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    here     = Path(__file__).parent
    cfg_path = here / "config.py"
    if not cfg_path.exists():
        print("ERROR: config.py が見つかりません")
        sys.exit(1)

    cfg_mod = _load_module("config", cfg_path)
    if cfg_mod is None:
        sys.exit(1)

    cfg_mod = apply_env_overrides(cfg_mod)

    for d in [cfg_mod.DATA_DIR, cfg_mod.RESULT_DIR, cfg_mod.INSIGHT_DIR,
              cfg_mod.ARXIV_DIR, cfg_mod.SECTOR_DIR, cfg_mod.MACRO_DIR,
              cfg_mod.LOG_DIR, cfg_mod.INSIGHT_DIR / "ideas",
              cfg_mod.INSIGHT_DIR / "macro"]:
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    main_path = here / "stock_analyzer_main.py"
    if not main_path.exists():
        print("ERROR: stock_analyzer_main.py が見つかりません")
        sys.exit(1)

    sys.modules["config"] = cfg_mod
    main_mod = _load_module("stock_analyzer_main", main_path)
    if main_mod is None:
        sys.exit(1)

    setattr(main_mod, "cfg", cfg_mod)

    # 単体テストモード: SINGLE_MODULE が設定されている場合は
    # DAILY_MODULES / WEEKLY_MODULES をそのモジュールだけに上書きする
    single_mod = os.environ.get("SINGLE_MODULE", "").strip()
    if single_mod:
        cfg_mod.DAILY_MODULES  = [single_mod] # type: ignore
        cfg_mod.WEEKLY_MODULES = [single_mod] # type: ignore
        args.mode = "all"   # all にすることで _in_mode() が必ず True を返す
        print(f"  🧪 単体テストモード: [{single_mod}] のみ実行")
    
    print(f"\n  RUN run_all_tasks(mode='{args.mode}') 開始...")
    try:
        results   = main_mod.run_all_tasks(mode=args.mode)
        # 注: anomaly_season / options_monitor は run_all_tasks 内でスケジュール
        #   （anomaly_season=週次 / options_monitor=日次）に従って
        #   run_anomaly_season() / run_options_monitor() で実行され results に格納済み。
        #   以前ここにあった「追加実行」ブロックは、存在しない run_analysis() を呼んで
        #   毎回 AttributeError（"module 'anomaly_season' has no attribute
        #   'run_analysis'"）を出すだけの壊れた二重実行だったため削除した。
        #   （幻のAPI / CLAUDE.md パターン②。正しい実行は run_all_tasks が担う。）
        n_sectors = len(results.get("sectors", {})) if isinstance(results, dict) else 0
        n_alerts  = (results.get("alerts", {}).get("alerts_sent", 0)
                     if isinstance(results, dict) else 0)
        print(f"\n  OK 完了: sectors={n_sectors} alerts={n_alerts}")
    except Exception as e:
        print(f"\n  ERROR 実行エラー: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
