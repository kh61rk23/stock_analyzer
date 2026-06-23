# ==============================================================
# devil_calibration.py  v1.0  (Wave DEPT-3)
# デビルアナリスト校正ノート生成
# ==============================================================
# 【役割】
#   投資部の過去実績（40_判断台帳・investment_journal成績・過去の
#   devil.md 判定）を集計し、デビルが次回審議で参照する
#   「25_デビル校正ノート.md」を vault に生成する。
#
# 【校正の思想】（SPEC_investment_dept.md DEPT-3）
#   デビルの検証は「過去にどの種類の論証がどれだけ外れたか」という
#   実績ベースの事前確率を持つことで精度が上がる。
#   例: 「Buy判断の的中率が conviction 4 で 55% なら、conviction 4 の
#       ブル仮説には自動的に懐疑の重みを置く」
#
# 【集計内容】
#   ① 判断分布と承認率（判断台帳）
#   ② conviction別・層別の成績（成績列が埋まっている行のみ）
#   ③ デビル判定（A/B/C）の分布と、未解消指摘数と成績の関係
#   ④ データ不足の明示（少数サンプルでの過信を防ぐ）
#
# 【実行】
#   python3 devil_calibration.py            # vault に校正ノート生成
#   ※ obsidian_sync.py all からも呼ばれる想定（DEPT-2連携）
# ==============================================================

from __future__ import annotations

import importlib.util
import json
import os
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).parent


def _load_config():
    spec = importlib.util.spec_from_file_location(
        "config", str(REPO / "config.py"))
    cfg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cfg)
    base_env = os.environ.get("STOCK_ANALYZER_BASE", "")
    base = Path(base_env) if base_env else (
        Path(cfg.DRIVE_BASE) if Path(cfg.DRIVE_BASE).exists()
        else REPO / "stock_data")
    cfg.DRIVE_BASE, cfg.DATA_DIR = base, base / "data"
    return cfg


def _parse_ledger(vault: Path) -> list[dict]:
    lp = vault / "40_判断台帳.md"
    if not lp.exists():
        return []
    spec = importlib.util.spec_from_file_location(
        "obsidian_sync", str(REPO / "obsidian_sync.py"))
    osync = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(osync)
    return osync._parse_ledger_rows(lp.read_text(encoding="utf-8"))


def _scan_devil_judgements(vault: Path) -> dict:
    """過去の devil.md から A/B/C 判定を集計する（best-effort）。"""
    counts = {"A": 0, "B": 0, "C": 0}
    memo_count = 0
    for devil_md in sorted(vault.glob("20_審議/*/devil.md")):
        try:
            text = devil_md.read_text(encoding="utf-8")
            memo_count += 1
            for m in re.finditer(r"判定[:：]\s*([ABC])", text):
                counts[m.group(1)] += 1
        except Exception:
            continue
    return {"memos": memo_count, "judgements": counts}


def _result_to_float(result: str):
    """成績文字列（例 '+12.3%'）を数値化。解析不能は None。"""
    m = re.search(r"([+-]?\d+(?:\.\d+)?)\s*%", str(result))
    return float(m.group(1)) if m else None


def build_note(vault: Path) -> str:
    rows = _parse_ledger(vault)
    devil = _scan_devil_judgements(vault)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    lines = [
        "# デビル校正ノート（自動生成）",
        "",
        f"> 生成: {now} / devil_calibration.py（手動編集禁止・毎回上書き）",
        "> B3デビルアナリストは審議開始時に本ノートを読み、",
        "> 過去実績ベースの事前確率を検証の重み付けに使うこと。",
        "",
        "## ① 審議実績サマリー",
        "",
    ]

    if not rows:
        lines += [
            "- **実績データなし（審議0件）**",
            "- → 校正材料が無いため、デビルは部則の横断チェックリストのみで",
            "  検証すること。**少数の初期審議結果から傾向を断定しないこと。**",
            "",
        ]
    else:
        verdicts = defaultdict(int)
        approved = 0
        for r in rows:
            verdicts[r.get("verdict", "?")] += 1
            if r.get("status") == "承認":
                approved += 1
        lines.append(f"- 総審議: {len(rows)}件 / 承認: {approved}件")
        lines.append("- 判断分布: " + " / ".join(
            f"{k} {v}件" for k, v in sorted(verdicts.items())))
        lines.append("")

        # conviction別成績
        by_conv = defaultdict(list)
        by_layer = defaultdict(list)
        scored = 0
        for r in rows:
            val = _result_to_float(r.get("result", ""))
            if val is None:
                continue
            scored += 1
            by_conv[r.get("conviction", "?")].append(val)
            by_layer[r.get("layer", "?")].append(val)

        lines.append("## ② conviction別・層別の成績")
        lines.append("")
        if scored == 0:
            lines.append("- 成績付与済みの審議なし（journal成績の還流待ち）。")
            lines.append("  → conviction の higher=better がまだ実証されていない点を")
            lines.append("  両アナリストの自己評価確度（confidence_pct）の検証に使うこと。")
        else:
            lines.append("| conviction | 件数 | 平均成績 | 勝率 |")
            lines.append("|---|---|---|---|")
            for conv in sorted(by_conv, reverse=True):
                vals = by_conv[conv]
                win = sum(1 for v in vals if v > 0)
                lines.append(
                    f"| {conv} | {len(vals)} | {sum(vals)/len(vals):+.1f}% "
                    f"| {win}/{len(vals)} |")
            lines.append("")
            lines.append("| 層 | 件数 | 平均成績 |")
            lines.append("|---|---|---|")
            for layer, vals in sorted(by_layer.items()):
                lines.append(
                    f"| {layer} | {len(vals)} | {sum(vals)/len(vals):+.1f}% |")
            if scored < 10:
                lines.append("")
                lines.append(f"- ⚠️ **サンプル{scored}件は統計的に不十分。**"
                             "傾向の断定ではなく仮説の参考に留めること。")
        lines.append("")

    lines += [
        "## ③ デビル自身の判定傾向",
        "",
        f"- 過去メモ: {devil['memos']}件 / 判定分布: "
        f"A {devil['judgements']['A']} / B {devil['judgements']['B']} / "
        f"C {devil['judgements']['C']}",
    ]
    j = devil["judgements"]
    total_j = sum(j.values())
    if total_j >= 10:
        a_rate = j["A"] / total_j
        if a_rate > 0.7:
            lines.append("- ⚠️ A判定率が7割超 — **検証が甘くなっていないか**を疑うこと")
        elif j["C"] / total_j > 0.5:
            lines.append("- ⚠️ C判定率が5割超 — 鮮度起因なら obsidian_sync の実行頻度を上げる提案を、"
                         "そうでなければ過剰懐疑を疑うこと")
        else:
            lines.append("- 判定バランスは健全域")
    else:
        lines.append("- 判定サンプル不足（10件未満）— 傾向分析は保留")
    lines += [
        "",
        "## ④ 本ノートの使い方（B3部則の補足）",
        "",
        "1. ②の実績と矛盾する確信度（例: 実績の無い conviction 5）には",
        "   1段階の懐疑を上乗せする",
        "2. ③で自分の判定が偏っていれば、今回の審議で意識的に補正する",
        "3. **本ノートは事前確率にすぎない。パケットの個別事実が常に優先。**",
        "",
    ]
    return "\n".join(lines)


def generate(vault_path: str = "") -> Path:
    cfg = _load_config()
    vault = Path(vault_path or getattr(cfg, "INVESTMENT_DEPT_VAULT_DIR", ""))
    if not vault.exists():
        raise SystemExit(f"❌ vault が見つかりません: {vault}")
    out = vault / "25_デビル校正ノート.md"
    out.write_text(build_note(vault), encoding="utf-8")
    print(f"✅ デビル校正ノート生成: {out}")
    return out


if __name__ == "__main__":
    import sys
    generate(sys.argv[1] if len(sys.argv) > 1 else "")
