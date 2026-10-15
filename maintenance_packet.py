# ==============================================================
# maintenance_packet.py  v1.0  (Wave AGENT-1)
# 保守セッションパケット自動生成 — AIエージェント自己保守の入口
# ==============================================================
# 【役割】
#   AIエージェント（Claude Code等）が保守セッションを開始する際に
#   読むべき「システムの現在地」を1ファイルに自動集約する:
#     ① 全監査の実行結果（config_audit / pipeline / results_schema / pytest）
#     ② 実行台帳の最新状態（連続失敗・配線漏れ）
#     ③ 開発ログ台帳（G-dev 40_開発ログ.jsonl）の直近エントリ（最近何が変わったか）
#     ④ 標準保守手順への参照
#
# 【実行】
#   python3 maintenance_packet.py
#   → MAINTENANCE_PACKET.md（リポジトリ直下）と、ドキュメントフォルダが
#     存在すればそちらにも出力
#
# 【設計思想】
#   2026-06 の経験則: AIセッションの事故は「現在地を知らずに改修を始める」
#   ことから起きる。本パケットを読了してから着手するのが部内規約
#   （CLAUDE.md 参照）。
# ==============================================================

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).parent
# 構成管理資料の正本は 02_Configuration_Management。保守パケットは進捗管理フォルダにも置く
DOCS = REPO.parent / "02_Configuration_Management" / "04_進捗管理"
# 変更履歴の正本（CHANGELOG.md は凍結済み）。board の devlog.py が追記する
DEVLOG = (REPO.parent.parent / "Vault2_ClaudeCode_Local" / "CEO室" / "departments"
          / "G-dev" / "40_開発ログ.jsonl")


def _run(cmd: list[str], timeout: int = 300) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, cwd=str(REPO))
        return r.returncode, (r.stdout + r.stderr).strip()
    except Exception as e:
        return 1, f"実行失敗: {e}"


def build_packet() -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "# 保守セッションパケット（自動生成）",
        "",
        f"> 生成: {now} / maintenance_packet.py（手動編集禁止）",
        "> AIエージェントは改修着手前に本パケットを読了すること。",
        "> 標準手順: リポジトリ直下 CLAUDE.md / 設計正本: SPEC_core.md",
        "",
        "## ① 監査結果",
        "",
    ]

    audits = [
        ("SSOT監査（config_audit）",
         [sys.executable, "-W", "ignore", "config_audit.py"]),
        ("パイプライン整合（pipeline_registry_gen --audit）",
         [sys.executable, "-W", "ignore", "pipeline_registry_gen.py", "--audit"]),
        ("results契約（results_schema --audit）",
         [sys.executable, "results_schema.py", "--audit"]),
        ("回帰テスト（pytest tests/）",
         [sys.executable, "-m", "pytest", "tests/", "-q", "--no-header"]),
    ]
    all_green = True
    for label, cmd in audits:
        code, out = _run(cmd)
        icon = "✅" if code == 0 else "❌"
        if code != 0:
            all_green = False
        tail = [l for l in out.splitlines() if l.strip()][-3:]
        lines.append(f"### {icon} {label}（exit {code}）")
        lines += [f"    {l}" for l in tail]
        lines.append("")

    lines.append(f"**総合判定: {'✅ 全監査グリーン — 改修着手可' if all_green else '❌ 監査NGあり — まず監査の修復から着手すること'}**")
    lines.append("")

    # ② 実行台帳
    lines += ["## ② 実行台帳（直近の本番実行状態）", ""]
    ledger_p = REPO / "stock_data" / "logs" / "module_ledger_latest.json"
    if ledger_p.exists():
        try:
            led = json.loads(ledger_p.read_text(encoding="utf-8"))
            s = led.get("stats", {})
            lines.append(f"- {led.get('date','')}: 成功{s.get('success',0)} "
                         f"/ 失敗{s.get('error',0)} / スキップ{s.get('skipped',0)}")
            for a in led.get("alerts", []):
                lines.append(f"- 🚨 連続失敗{a['days']}日: {a['module']}")
            if led.get("unrecorded"):
                lines.append(f"- 👻 記録なし: {', '.join(led['unrecorded'][:8])}")
        except Exception:
            lines.append("- ⚠️ 台帳解析エラー")
    else:
        lines.append("- 台帳なし（本番実行ログが未取得。obsidian_sync pull で取得可）")
    lines.append("")

    # ③ 開発ログ台帳 直近
    lines += ["## ③ 最近の変更（開発ログ台帳 直近10件）", ""]
    if DEVLOG.exists():
        try:
            rows = []
            for l in DEVLOG.read_text(encoding="utf-8").splitlines():
                if not l.strip():
                    continue
                e = json.loads(l)
                if e.get("project") == "stock_analyzer":
                    rows.append(e)
            rows.sort(key=lambda e: (e.get("date", ""), e.get("added", "")), reverse=True)
            lines += [f"- [{e.get('date', '')}] {e.get('kind', '')}: {e.get('title', '')}"
                      + (f"（{e['commit']}）" if e.get("commit") else "")
                      for e in rows[:10]] or ["- エントリなし"]
        except Exception:
            lines.append("- ⚠️ 読込エラー")
    else:
        lines.append(f"- 開発ログ台帳なし（探索: {DEVLOG}）")
    lines += [
        "",
        "## ④ 改修時の鉄則（要約 — 詳細は CLAUDE.md）",
        "",
        "1. `stock_analyzer-main_bk` は絶対に触らない",
        "2. config.py 変更後は `python3 config_audit.py`（重複定義は後勝ち上書きで黙って壊れる）",
        "3. main 変更後は `python3 pipeline_registry_gen.py` で registry 再生成",
        "4. results キー追加時は results_schema.py の SCHEMA に追記",
        "5. 改修完了時は pytest 全通過を確認し、開発ログ台帳（devlog.py add）と構成管理資料を更新して tools/sync_config_docs.py で push",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    md = build_packet()
    out1 = REPO / "MAINTENANCE_PACKET.md"
    out1.write_text(md, encoding="utf-8")
    print(f"✅ 保守パケット生成: {out1}")
    if DOCS.exists():
        out2 = DOCS / "MAINTENANCE_PACKET.md"
        out2.write_text(md, encoding="utf-8")
        print(f"✅ ドキュメントフォルダにも出力: {out2}")
    return 0 if "✅ 全監査グリーン" in md else 1


if __name__ == "__main__":
    sys.exit(main())
