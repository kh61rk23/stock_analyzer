# ==============================================================
# geo_exposure_runner.py  v1.0  (2026-07-25 / Wave 6 - G-C 配線復元)
# 地政学シグナルの「供給側」3抽出器の定期実行
# ==============================================================
# 【役割】
#   geopolitics.company_country_exposure（企業×国×資源の権益テーブル）を
#   埋める3つの抽出器を、それぞれの実行間隔にしたがって呼ぶ。
#     1) jogmec_scraper.py            … JOGMEC 権益DB（手動マスター＋RSS差分）
#     2) sec_geographic_extractor.py  … SEC 10-K の地域別売上
#     3) edinet_exposure_extractor.py … EDINET 有報の海外権益テキスト
#
# 【背景 — なぜ新設したか】
#   地政学シグナル（Wave 6 - G-X）は消費側が完成していた:
#     base_agent / us_stock_screener  → calc_geo_impact_bonus_*()
#     signal_aggregator               → aggregate_geo_signal()（重み4%）
#     weekly_report_generator         → geo_impact_bonus ランキング
#   リスク側（country_risk_updater）も日次で配線済み。
#   ところが「企業がどの国に権益を持つか」を埋める上記3本だけが、
#   GitHub Actions にも launchd にも run_all_tasks() にも呼び出しが無く、
#   exposure テーブルが空のままだった。
#   join 相手が無いので geo_impact_bonus は常に 0 → ENABLE_GEO_SIGNAL=False
#   という「結果としての停止」になっていた（2026-07-25 の棚卸しで判明）。
#   config.py のコメントは "run_all_tasks() での jogmec_scraper 実行を
#   スキップ" と書いており、本来は日次パイプラインに載る設計だった。
#
#   日次ではなく月次相当（30日）で回すのは config.py が定める間隔に従うため。
#   SEC/EDINET の有報は年1回しか更新されず、毎日叩く意味が無い。
#
# 【実行】
#   python3 geo_exposure_runner.py [--dry-run] [--force] [--only <name>]
#     --force : 実行間隔を無視して全部走らせる
#     --only  : jogmec / sec / edinet のいずれかだけ実行
#     --dry-run: 各抽出器に dry_run=True を渡す（BQ書込なし）
#
# 【呼ばれ方】monthly_runner.py の geo_exposure ステップ（月初）
# 【設定】config.py Wave 6 - G-C ブロック
#   ENABLE_JOGMEC_SCRAPER / JOGMEC_SCRAPER_INTERVAL_DAYS
#   ENABLE_SEC_GEO_EXTRACTOR / SEC_GEO_EXTRACTOR_INTERVAL_DAYS
#   ENABLE_EDINET_EXPOSURE_EXTRACTOR / EDINET_EXPOSURE_INTERVAL_DAYS
# 【前提】BQ 認証（BQ_CREDENTIALS）。未設定時は各抽出器が success=False を
#   返すのでこのランナーは警告を出して続行する（日次分析は止めない）。
# ==============================================================

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
from datetime import date, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent

# (キー, モジュール名, run関数名, ENABLEフラグ, 間隔フラグ, 既定間隔)
EXTRACTORS = [
    ("jogmec", "jogmec_scraper", "run_jogmec_scraper",
     "ENABLE_JOGMEC_SCRAPER", "JOGMEC_SCRAPER_INTERVAL_DAYS", 30),
    ("sec", "sec_geographic_extractor", "run_sec_geographic_extractor",
     "ENABLE_SEC_GEO_EXTRACTOR", "SEC_GEO_EXTRACTOR_INTERVAL_DAYS", 30),
    ("edinet", "edinet_exposure_extractor", "run_edinet_exposure_extractor",
     "ENABLE_EDINET_EXPOSURE_EXTRACTOR", "EDINET_EXPOSURE_INTERVAL_DAYS", 30),
]


def _load_config():
    try:
        spec = importlib.util.spec_from_file_location(
            "config", str(REPO / "config.py"))
        cfg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cfg)
        return cfg
    except Exception as e:
        print(f"⚠️  config.py 読込失敗（既定値で継続）: {str(e)[:120]}")
        return None


def _state_path(cfg) -> Path:
    base = Path(getattr(cfg, "DATA_DIR", REPO / "stock_data" / "data"))
    return base / "geo_exposure_state.json"


def _load_state(cfg) -> dict:
    try:
        return json.loads(_state_path(cfg).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(cfg, state: dict) -> None:
    p = _state_path(cfg)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    except Exception as e:
        print(f"  ⚠️ 実行日記録に失敗: {str(e)[:60]}")


def due(state: dict, key: str, interval_days: int,
        today: date = None) -> tuple:
    """実行間隔の判定。Returns: (走らせるか, 理由)

    前回実行の記録が無い初回は必ず走らせる（テーブルが空のままなので）。
    """
    today = today or date.today()
    last_s = (state.get(key) or {}).get("last_run", "")
    try:
        last = date.fromisoformat(str(last_s))
    except Exception:
        return True, "前回実行の記録なし（初回）"
    elapsed = (today - last).days
    if elapsed >= interval_days:
        return True, f"前回から{elapsed}日経過（間隔{interval_days}日）"
    return False, f"前回から{elapsed}日（間隔{interval_days}日に未達）"


def run(cfg=None, dry_run: bool = False, force: bool = False,
        only: str = "") -> dict:
    """3抽出器を間隔判定つきで実行する。Returns: 実行サマリ dict"""
    cfg = cfg or _load_config()
    project_id = getattr(cfg, "GEO_BQ_PROJECT_ID", "stock-analyzer-edinet")
    state = _load_state(cfg)
    today = date.today()

    summary = {"run_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
               "steps": {}, "ok": True, "merged_total": 0}

    for key, mod_name, fn_name, enable_flag, interval_flag, default_iv in EXTRACTORS:
        if only and only != key:
            continue

        if not getattr(cfg, enable_flag, True):
            print(f"  ⏩ {key}: スキップ（{enable_flag}=False）")
            summary["steps"][key] = f"skipped / {enable_flag}=False"
            continue

        interval = int(getattr(cfg, interval_flag, default_iv))
        if not force:
            ok, why = due(state, key, interval, today)
            if not ok:
                print(f"  ⏩ {key}: スキップ（{why}）")
                summary["steps"][key] = f"skipped / {why}"
                continue

        try:
            mod = importlib.import_module(mod_name)
            fn = getattr(mod, fn_name)
            result = fn(cfg=cfg, project_id=project_id, dry_run=dry_run) or {}
        except Exception as e:
            # 1本落ちても他は続ける。exposure は3ソースの和集合なので
            # 部分的にでも埋まる方が geo_signal にとって有益。
            summary["ok"] = False
            summary["steps"][key] = f"error: {str(e)[:150]}"
            print(f"  ❌ {key}: {str(e)[:150]}")
            continue

        merged = int(result.get("merged", 0) or 0)
        total = int(result.get("total", 0) or 0)
        if result.get("success"):
            summary["steps"][key] = f"success / {merged}件MERGE（抽出{total}件）"
            summary["merged_total"] += merged
            print(f"  ✅ {key}: {merged}件MERGE（抽出{total}件）")
            if not dry_run:
                state[key] = {"last_run": today.isoformat(),
                              "merged": merged, "total": total}
                # 途中で打ち切られても済んだ抽出器はやり直さない（2026-10-01）
                _save_state(cfg, state)
        else:
            # BQ認証なし等。日次分析は止めないので ok は落とさず warn 扱い。
            err = str(result.get("error", ""))[:150]
            summary["steps"][key] = f"warn / {err or 'success=False'}"
            print(f"  ⚠️ {key}: {err or 'success=False'}")

    if not dry_run:
        _save_state(cfg, state)
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="地政学シグナルの供給側3抽出器を実行する")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="実行間隔を無視して実行する")
    ap.add_argument("--only", default="",
                    choices=["", "jogmec", "sec", "edinet"])
    args = ap.parse_args(argv)

    print("🌍 geo_exposure_runner: 権益エクスポージャー抽出")
    s = run(dry_run=args.dry_run, force=args.force, only=args.only)
    print(("✅ 完了" if s["ok"] else "⚠️ 一部失敗")
          + f" / 合計 {s['merged_total']}件MERGE")
    return 0 if s["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
