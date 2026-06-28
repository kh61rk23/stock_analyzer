# ==============================================================
# run_headless.py  v2.0  (OAuth対応・Google Drive保存)
# ==============================================================
# 【v1.0 → v2.0 変更点】
#   Google Drive への結果保存機能を追加（OAuth認証方式）
#   サービスアカウント方式は storageQuotaExceeded エラーが出るため廃止
#
# 【必要な GitHub Secrets】
#   既存:
#     ALERT_GMAIL_FROM / ALERT_GMAIL_PASSWORD / ALERT_GMAIL_TO
#     ALERT_LINE_TOKEN / ALERT_LINE_USER_ID（任意）
#     EDINET_API_KEY / POLICY_FRED_API_KEY（任意）
#   追加（v2.0）:
#     GOOGLE_OAUTH_CLIENT_ID      : OAuthクライアントID
#     GOOGLE_OAUTH_CLIENT_SECRET  : OAuthクライアントシークレット
#     GOOGLE_OAUTH_REFRESH_TOKEN  : リフレッシュトークン
#     GOOGLE_DRIVE_FOLDER_ID      : 保存先フォルダID（既存のまま）
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
        "EDINET_API_KEY":             "EDINET_API_KEY",
        "EDINETDB_API_KEY":           "EDINETDB_API_KEY",
        "POLICY_FRED_API_KEY":        "POLICY_FRED_API_KEY",
        "ANTHROPIC_API_KEY":          "ANTHROPIC_API_KEY",
        "SEC_USER_AGENT":             "SEC_USER_AGENT",
        "GOOGLE_CSE_API_KEY":         "GOOGLE_CSE_API_KEY",
        "GOOGLE_CSE_ID":              "GOOGLE_CSE_ID",
        "GDRIVE_DASHBOARD_FILE_ID":  "GDRIVE_DASHBOARD_FILE_ID",
        "DEEPL_API_KEY":              "DEEPL_API_KEY",
        "ESTAT_API_KEY":              "ESTAT_API_KEY",
        "GOOGLE_OAUTH_CLIENT_ID":     "GOOGLE_OAUTH_CLIENT_ID",
        "GOOGLE_OAUTH_CLIENT_SECRET": "GOOGLE_OAUTH_CLIENT_SECRET",
        "GOOGLE_OAUTH_REFRESH_TOKEN": "GOOGLE_OAUTH_REFRESH_TOKEN",
        "GOOGLE_DRIVE_FOLDER_ID":     "GOOGLE_DRIVE_FOLDER_ID",
        "NOTION_API_KEY":             "NOTION_API_KEY",  # 2026-06-10 ハードコード廃止に伴い追加
        "CONGRESS_API_KEY":           "CONGRESS_API_KEY",  # 2026-06-10 congress_monitor配線復元に伴い追加
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
    # ★ DRIVE_BASE 由来で取りこぼしていたパスもここで揃える。
    #   特に CACHE_DIR は score_prior / policy_radar 等 10+ モジュールが参照し、
    #   取りこぼすと Colab パス /content に書こうとして Permission denied になる
    #   （2026-06-25 本番で score_prior が全業種無効化した実害）。
    #   config.py も STOCK_ANALYZER_BASE を見るようにしたが、env 未設定で
    #   base が __file__/stock_data に決まるローカル実行でも整合させるため明示する。
    cfg.CACHE_DIR             = base / "cache"
    cfg.SUBTHEME_JSON_PATH_JP = base / "data" / "jp_sector_subtheme.json"
    cfg.SUBTHEME_JSON_PATH_US = base / "data" / "us_sector_subtheme.json"

    if overridden:
        print(f"  KEY 環境変数を適用: {', '.join(overridden)}")
    print(f"  DIR BASE_DIR: {base}")
    return cfg

# OAuth認証を用いてGoogle Drive APIの操作環境を構築・実行する関数
def _upload_to_gdrive_oauth(client_id, client_secret, refresh_token,
                             folder_id, insight_dir, log_dir):
    try:
        from google.oauth2.credentials import Credentials # type: ignore
        from google.auth.transport.requests import Request # type: ignore
        from googleapiclient.discovery import build # type: ignore
        from googleapiclient.http import MediaFileUpload # type: ignore
    except ImportError:
        import subprocess
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-q",
             "google-api-python-client", "google-auth-httplib2",
             "google-auth-oauthlib"],
            capture_output=True
        )
        from google.oauth2.credentials import Credentials # type: ignore
        from google.auth.transport.requests import Request # type: ignore
        from googleapiclient.discovery import build # type: ignore
        from googleapiclient.http import MediaFileUpload # type: ignore

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=["https://www.googleapis.com/auth/drive"],
    )
    creds.refresh(Request())
    service = build("drive", "v3", credentials=creds)

    #グーグルドライブの既存フォルダの特定、または新規作成によるID取得をする関数
    def _get_or_create_folder(name, parent_id):
        q = (f"name='{name}' and "
             f"mimeType='application/vnd.google-apps.folder' and "
             f"'{parent_id}' in parents and trashed=false")
        res = service.files().list(q=q, fields="files(id)").execute()
        files = res.get("files", [])
        if files:
            return files[0]["id"]
        meta = {"name": name,
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [parent_id]}
        return service.files().create(body=meta, fields="id").execute()["id"]

    #グーグルドライブに既存確認を伴うファイルの新規作成または更新（同期）する関数
    def _upload_file(local_path, parent_id):
        name = local_path.name
        q = (f"name='{name}' and '{parent_id}' in parents and trashed=false")
        res = service.files().list(q=q, fields="files(id)").execute()
        files = res.get("files", [])
        media = MediaFileUpload(str(local_path))
        if files:
            service.files().update(
                fileId=files[0]["id"], media_body=media).execute()
        else:
            service.files().create(
                body={"name": name, "parents": [parent_id]},
                media_body=media, fields="id").execute()

    total = 0
    for local_base, remote_name in [(insight_dir, "insights"),
                                     (log_dir, "logs")]:
        if not Path(local_base).exists():
            continue
        base_id = _get_or_create_folder(remote_name, folder_id)
        for f in Path(local_base).rglob("*"):
            if not f.is_file():
                continue
            rel = f.relative_to(local_base)
            pid = base_id
            for part in rel.parts[:-1]:
                pid = _get_or_create_folder(part, pid)
            _upload_file(f, pid)
            total += 1

    print(f"  OK Google Drive アップロード完了: {total} ファイル")

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
        try:
            import anomaly_season
            import options_monitor
            if isinstance(results, dict):
                # モジュールを実行し、結果を dict に格納する
                results["anomaly_season"] = anomaly_season.run_analysis(cfg_mod) # type: ignore
                results["options_monitor"] = options_monitor.run_analysis(cfg_mod) # type: ignore
        except Exception as e:
            print(f"  WARNING 分析モジュールの追加実行に失敗しました: {e}")
        n_sectors = len(results.get("sectors", {})) if isinstance(results, dict) else 0
        n_alerts  = (results.get("alerts", {}).get("alerts_sent", 0)
                     if isinstance(results, dict) else 0)
        print(f"\n  OK 完了: sectors={n_sectors} alerts={n_alerts}")
    except Exception as e:
        print(f"\n  ERROR 実行エラー: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    # Google Drive アップロード（OAuth方式）
    client_id     = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
    refresh_token = os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN", "")
    folder_id     = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "")

    if all([client_id, client_secret, refresh_token, folder_id]):
        print("\n  UPLOAD Google Drive にアップロード中...")
        try:
            _upload_to_gdrive_oauth(
                client_id=client_id, client_secret=client_secret,
                refresh_token=refresh_token, folder_id=folder_id,
                insight_dir=cfg_mod.INSIGHT_DIR, log_dir=cfg_mod.LOG_DIR,
            )
        except Exception as e:
            print(f"  WARNING Google Drive アップロードエラー: {e}")
    else:
        missing = [k for k, v in {
            "GOOGLE_OAUTH_CLIENT_ID":     client_id,
            "GOOGLE_OAUTH_CLIENT_SECRET": client_secret,
            "GOOGLE_OAUTH_REFRESH_TOKEN": refresh_token,
            "GOOGLE_DRIVE_FOLDER_ID":     folder_id,
        }.items() if not v]
        print(f"\n  SKIP Google Drive スキップ（未設定: {', '.join(missing)}）")

    sys.exit(0)


if __name__ == "__main__":
    main()
