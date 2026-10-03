# ==============================================================
# monthly_runner.py  v1.0  (2026-07-25 / B-5)
# 月次・四半期モジュールの一括実行
# ==============================================================
# 【役割】
#   月初に走る「提案・レビュー系」を1本に束ねて連続実行する。
#     1) signal_culling_report.py   … E4 シグナル淘汰レポート（IC→重み提案）
#     2) paper/monthly_report.py    … S2 戦略別月次成績
#     3) allocation_meta.py         … S11 分数ケリー×リスクパリティ配分提案
#     4) commodity_rotation.py      … S6 コモディティ配分提案（当月ぶん）
#     5) tax_optimizer.py           … S10 税・NISA最適化（四半期＋11月のみ）
#     6) monthly_strategy_review.py … E12 月次戦略レビュー（上記を集約＝最後）
#
#   実行順は依存関係で決まっている。⑥は①のレポートと②の口座成績を読むため
#   必ず最後。③は②が更新した accounts.json を読む。
#
# 【背景】2026-07-25 の棚卸しで、これらは設計・実装・テストまで揃っているのに
#   GitHub Actions にも launchd にも実行主体が無く、一度も走っていなかった。
#   個別に6ステップを並べるのではなく1本にまとめたのは、実行順の依存を
#   ここだけで保証し、観測点（monthly_loop_status.json）を1つにするため。
#
# 【実行】
#   python3 monthly_runner.py [--month YYYYMM] [--dry-run] [--force]
#     --month  : 対象月。既定は「前月」（月初1日に閉じたばかりの月を見る運用）
#     --force  : 月初判定を無視して実行（手動での追いつき用）
#     --dry-run: 実行せずコマンドだけ表示
#
# 【呼ばれ方】
#   - GitHub Actions : .github/workflows/daily_run.yml の月初判定ステップ
#   - ローカル(Mac)  : 司令塔シェル run_apps.sh invest-monthly（毎月1日 21:30）
#   両方から走っても各モジュールは同じ月のファイルを上書きするだけで冪等。
#
# 【設定】config.py BLOCK 58
#   ENABLE_MONTHLY_RUNNER / ENABLE_SIGNAL_CULLING /
#   ENABLE_MONTHLY_STRATEGY_REVIEW / TAX_OPTIMIZER_MONTHS / MONTHLY_RUN_MAX_DAY
# ==============================================================

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent
STATUS_PATH = REPO / "stock_data" / "monthly_loop_status.json"


def _load_config():
    """config.py を単体で読む（run_headless を経由しない直接起動のため）。"""
    try:
        spec = importlib.util.spec_from_file_location(
            "config", str(REPO / "config.py"))
        cfg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cfg)
        return cfg
    except Exception as e:
        print(f"⚠️  config.py 読込失敗（既定値で継続）: {str(e)[:120]}")
        return None


def prev_month(today: date) -> str:
    """前月を YYYYMM で返す。"""
    y, m = today.year, today.month - 1
    if m == 0:
        y, m = y - 1, 12
    return f"{y}{m:02d}"


def build_steps(cfg, yyyymm: str, today: date) -> list:
    """(ステップ名, 実行するか, コマンド, スキップ理由) のリストを組む。

    実行順＝依存順。並べ替えると monthly_strategy_review が
    signal_culling のレポートを読めなくなる。
    """
    py = sys.executable
    tax_months = tuple(getattr(cfg, "TAX_OPTIMIZER_MONTHS", (1, 4, 7, 10, 11)))

    def flag(name: str, default: bool = True) -> bool:
        return bool(getattr(cfg, name, default))

    return [
        # データ生成系を先に。geo は権益テーブル（BQ）を埋めるだけで
        # 下のレポート群とは独立だが、性質が「収集」なので先頭に置く。
        # 実行間隔（30日）の判定は geo_exposure_runner 側が持つ。
        ("geo_exposure",
         flag("ENABLE_JOGMEC_SCRAPER") or flag("ENABLE_SEC_GEO_EXTRACTOR")
         or flag("ENABLE_EDINET_EXPOSURE_EXTRACTOR"),
         [py, "geo_exposure_runner.py"],
         "geo系3フラグが全てFalse"),

        ("signal_culling",
         flag("ENABLE_SIGNAL_CULLING"),
         [py, "signal_culling_report.py", yyyymm],
         "ENABLE_SIGNAL_CULLING=False"),

        ("paper_monthly",
         flag("ENABLE_PAPER_TRADING"),
         [py, "paper/monthly_report.py", yyyymm],
         "ENABLE_PAPER_TRADING=False"),

        ("allocation_meta",
         flag("ENABLE_ALLOCATION_META"),
         [py, "allocation_meta.py", yyyymm],
         "ENABLE_ALLOCATION_META=False"),

        # 当月ぶんの配分提案（これだけ前月ではなく「これから」を見る）
        ("commodity_rotation",
         flag("ENABLE_COMMODITY_ROTATION"),
         [py, "commodity_rotation.py"],
         "ENABLE_COMMODITY_ROTATION=False"),

        ("tax_optimizer",
         flag("ENABLE_TAX_OPTIMIZER") and today.month in tax_months,
         [py, "tax_optimizer.py", str(today.year)],
         (f"対象外月（実行月: {tax_months}）"
          if flag("ENABLE_TAX_OPTIMIZER") else "ENABLE_TAX_OPTIMIZER=False")),

        # 集約系。上のステップの出力を読むため必ず最後。
        ("strategy_review",
         flag("ENABLE_MONTHLY_STRATEGY_REVIEW"),
         [py, "monthly_strategy_review.py", yyyymm],
         "ENABLE_MONTHLY_STRATEGY_REVIEW=False"),
    ]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="月次・四半期モジュールの一括実行")
    ap.add_argument("--month", default="", help="対象月 YYYYMM（既定: 前月）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="月初判定を無視して実行する")
    args = ap.parse_args(argv)

    cfg = _load_config()
    today = date.today()

    if not bool(getattr(cfg, "ENABLE_MONTHLY_RUNNER", True)):
        print("⏩ 月次ランナー: スキップ（ENABLE_MONTHLY_RUNNER=False）")
        return 0

    # 月初判定。Actions が1日に落ちても翌日以降に追いつけるよう幅を持たせる。
    max_day = int(getattr(cfg, "MONTHLY_RUN_MAX_DAY", 3))
    if not args.force and today.day > max_day:
        print(f"⏩ 月次ランナー: スキップ（{today.day}日 > 月初{max_day}日まで）"
              f" — 手動実行は --force")
        return 0

    yyyymm = args.month or prev_month(today)
    steps = build_steps(cfg, yyyymm, today)

    # 2026-10-03: 月初3日間は毎日すべてのステップを再実行していた（10/1〜10/3 で3回）。
    # 同じ対象月で成功済みのステップは飛ばし、失敗・未実行のものだけやり直す。
    prev = {}
    if not args.force and not args.dry_run:
        try:
            _p = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
            if _p.get("target_month") == yyyymm:
                prev = _p.get("steps") or {}
        except (OSError, ValueError):
            prev = {}
    done = {n for n, v in prev.items() if str(v).startswith(("success", "skipped"))}
    if steps and all(name in done for name, *_ in steps):
        print(f"⏩ 月次ランナー: 対象月={yyyymm} は全ステップ完了済み — 再実行は --force")
        return 0

    print(f"📅 月次ランナー開始: 対象月={yyyymm} / 実行日={today}"
          + (f"（完了済み{len(done)}件は飛ばす）" if done else ""))
    status = {
        "run_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "target_month": yyyymm,
        "steps": {},
        "ok": True,
    }

    for name, enabled, cmd, skip_reason in steps:
        if name in done:
            status["steps"][name] = prev[name]
            continue
        if not enabled:
            print(f"  ⏩ {name}: スキップ（{skip_reason}）")
            status["steps"][name] = f"skipped / {skip_reason}"
            continue
        if args.dry_run:
            print(f"  [dry-run] {name}: {' '.join(cmd)}")
            status["steps"][name] = "dry-run"
            continue
        try:
            r = subprocess.run(cmd, cwd=REPO, capture_output=True,
                               text=True, timeout=900)
            ok = (r.returncode == 0)
            status["steps"][name] = "success" if ok else f"exit {r.returncode}"
            if ok:
                print(f"  ✅ {name}")
            else:
                # 1本失敗しても後続は続ける。月次は独立性が高く、
                # 全体を止めるとレビューまで丸ごと落ちるため。
                status["ok"] = False
                tail = (r.stderr or r.stdout or "")[-300:]
                status["steps"][name] += f" / {tail}"
                print(f"  ❌ {name}: exit {r.returncode}\n{tail}")
        except subprocess.TimeoutExpired as e:
            # 2026-10-01: 打ち切り時に出力を捨てていたため、どこで止まったか分からなかった
            status["ok"] = False
            out = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            status["steps"][name] = f"timeout {e.timeout}s / {out[-300:]}"
            print(f"  ❌ {name}: {e.timeout}秒で打ち切り\n{out[-600:]}")
        except Exception as e:
            status["ok"] = False
            status["steps"][name] = f"error: {str(e)[:120]}"
            print(f"  ❌ {name}: {e}")

    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(
        json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    print(("✅ 月次ループ完了" if status["ok"] else "⚠️ 月次ループ一部失敗")
          + f" → {STATUS_PATH.relative_to(REPO)}")
    return 0 if status["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
