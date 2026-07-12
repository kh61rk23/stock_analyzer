# ==============================================================
# deliberation_scheduler.py — B部門隔週自動審議の準備（Wave R3-2 / E3）
# ==============================================================
# 【役割】隔週日曜のCowork審議を「承認だけで走る」状態にする軽量版
#   （DR3-1推奨案: 手順書＋プロンプトテンプレ＋packet連携まで。
#   スケジュール登録＝Coworkの定期実行設定は健太が行う）。
# 【処理】
#   ① 審議対象1銘柄を選定（screener上位×未審議＝40_判断台帳.md 除外）
#   ② agent_packet_exporter v2 で審議パケット生成（ontology＋E8込み）
#   ③ B1〜B4（ブル/ベア/デビル/レポーター）の起動プロンプトを
#      20_審議/{case_id}/_起動プロンプト.md に出力（相互不可視の指示込み）
#   ④ 次アクション（Coworkでサブエージェント起動）を提示
# 【モデル既定 D-12】ブル/ベア=Sonnet 5、デビル/レポーター=Opus 4.8（Fable温存）。
#   コスト＝Coworkサブスク内・API課金なし（費用ゼロ原則）。
# 使い方（Coworkスケジュール or 手動）:
#   python3 deliberation_scheduler.py            # 対象選定→packet→プロンプト生成
#   python3 deliberation_scheduler.py --ticker 7203.T   # 対象を明示指定
#   python3 deliberation_scheduler.py --dry-run  # 選定のみ（vault書込なし）
# ==============================================================

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


# ── 純粋ロジック（pytest対象） ────────────────────────────────

def select_deliberation_target(candidates: list, already: set) -> dict | None:
    """
    composite_score 降順で「未審議」の先頭候補を返す。
    candidates: [{"ticker","composite_score","sector_code",...}]
    already: 既に審議済みの ticker 集合（40_判断台帳から抽出）
    Returns: 選定候補dict / なければ None
    """
    ranked = sorted(
        (c for c in candidates
         if c.get("ticker") and c.get("composite_score") is not None
         and c["ticker"] not in already),
        key=lambda c: -float(c["composite_score"]))
    return ranked[0] if ranked else None


def parse_deliberated_tickers(ledger_text: str) -> set:
    """40_判断台帳.md から既審議 ticker を抽出（wikilink/コード表記の両対応）。"""
    if not ledger_text:
        return set()
    # 例: | 2026-07-01 | [[7203.T]] | Buy | ... / または 7203 単体
    tickers = set(re.findall(r"\b(\d{4}\.T|\d{4}|[A-Z]{1,5})\b", ledger_text))
    # 明らかな非ティッカー語（見出し等）は4桁数字/.T/2-5大文字に限定済み
    return tickers


# ── プロンプトテンプレ（D-12モデル既定） ─────────────────────

_COMMON = ("あなたはB投資部の担当アナリストです。審議パケット（唯一の数値正本）と"
           "自分の部則のみを読み、他アナリストのメモは見ないこと（相互不可視）。"
           "結論は末尾YAML（conviction 1-5 / 確度% / 主要論点ID）で構造化する。")

PROMPTS = {
    "bull": {
        "model": "Sonnet 5",
        "role": "①ブルアナリスト",
        "prompt": _COMMON + " あなたは強気仮説のみを構築する。リスクは書かない"
                  "（それはベア/デビルの職掌）。packetの数値を根拠に上昇シナリオを提示。"
                  "出力: 20_審議/{case_id}/01_ブル.md",
    },
    "bear": {
        "model": "Sonnet 5",
        "role": "②ベアアナリスト",
        "prompt": _COMMON + " あなたは弱気仮説のみを構築する。強気材料は書かない。"
                  "packetの数値を根拠に下落・停滞シナリオを提示。"
                  "出力: 20_審議/{case_id}/02_ベア.md",
    },
    "devil": {
        "model": "Opus 4.8",
        "role": "③デビルアナリスト",
        "prompt": _COMMON + " あなたは検証者。bull.md と bear.md と packet を突き合わせ、"
                  "両仮説の論証の弱点・反証・データ鮮度/二重計上を指摘し、論点別に信頼度A/B/Cを付す。"
                  "独自の相場観や新規の買い/売り理由は持ち込まない。"
                  "出力: 20_審議/{case_id}/03_デビル.md",
    },
    "reporter": {
        "model": "Opus 4.8",
        "role": "④レポートエージェント",
        "prompt": _COMMON + " あなたは統合者。3メモ＋packet＋strategy分類から最終判断"
                  "（Buy/Watch/Avoid＋conviction）・両論要約・デビル指摘の解消状況表・"
                  "推奨サイズ（position_sizer v2の内訳参照）・人間承認欄を作る。"
                  "3メモにない新規論拠は追加しない。"
                  "出力: 30_レポート/{case_id}_判断レポート.md ＋ 40_判断台帳.md 追記",
    },
}


def build_launch_prompts(case_id: str, ticker: str) -> str:
    """B1〜B4の起動プロンプトMDを組み立てる（純粋関数）。"""
    lines = [f"# 審議起動プロンプト: {ticker}（case: {case_id}）",
             "",
             "> 隔週自動審議（E3）の起動手順。**Coworkで下記を順に実行**。",
             "> ①②は相互不可視で**並列**起動可（各エージェントに自分の部則＋packetのみ渡す）。",
             "> ③は①②完了後、④は③完了後（順序厳守）。API課金なし＝Coworkサブスク内。",
             ""]
    order = [("bull", "並列可"), ("bear", "並列可"),
             ("devil", "①②完了後"), ("reporter", "③完了後")]
    for key, timing in order:
        p = PROMPTS[key]
        lines += [f"## {p['role']}  （モデル: **{p['model']}** / {timing}）",
                  "```",
                  p["prompt"].replace("{case_id}", case_id),
                  "```",
                  ""]
    lines += ["## 完了後",
              "- ⑤ 人間承認欄付きレポートを健太が確認（Buy/Watch/Avoid）",
              "- ⑥ 完了を翌朝ブリーフに掲載（未完了なら『審議未完了』表示）",
              "- 途中停止時は途中成果物を 20_審議/ に残し、翌日再開する"]
    return "\n".join(lines) + "\n"


# ── I/O層 ────────────────────────────────────────────────────

def _load_screener_candidates(cfg) -> list:
    data_dir = Path(getattr(cfg, "DATA_DIR", "stock_data/data"))
    sh = data_dir / "screener_history.json"
    try:
        data = json.loads(sh.read_text(encoding="utf-8"))
        for d in sorted(data.keys(), reverse=True):
            cands = (data[d] or {}).get("candidates", [])
            if cands:
                return cands
    except Exception:
        pass
    return []


def _load_ledger_tickers(cfg) -> set:
    vault = Path(getattr(cfg, "INVESTMENT_DEPT_VAULT_DIR", ""))
    ledger = vault / "40_判断台帳.md"
    try:
        return parse_deliberated_tickers(ledger.read_text(encoding="utf-8"))
    except Exception:
        return set()


def run_prep(cfg, ticker: str = "", dry_run: bool = False) -> dict:
    """メインエントリ。Returns: {"ticker","case_id","packet","prompts_path","skipped"}"""
    if not ticker:
        cands = _load_screener_candidates(cfg)
        already = _load_ledger_tickers(cfg)
        target = select_deliberation_target(cands, already)
        if not target:
            print("  ⏩ 審議対象なし（screener候補が空 or 全て審議済み）")
            return {"skipped": True}
        ticker = target["ticker"]
        print(f"  🎯 審議対象選定: {ticker}（composite={target.get('composite_score')}）")

    case_id = f"{datetime.now().strftime('%Y-%m-%d')}_{ticker.replace('.', '')}"
    prompts_md = build_launch_prompts(case_id, ticker)

    if dry_run:
        print(f"  ✅ --dry-run: 対象={ticker} case={case_id}（vault書込なし）")
        return {"ticker": ticker, "case_id": case_id, "skipped": False,
                "dry_run": True}

    # packet生成（best-effort）
    packet_path = None
    try:
        import agent_packet_exporter as ape
        packet_path = str(ape.export(ticker))
    except SystemExit as e:
        print(f"  ⚠️ packet生成スキップ（vault未検出）: {str(e)[:60]}")
    except Exception as e:
        print(f"  ⚠️ packet生成エラー: {str(e)[:60]}")

    # 起動プロンプトを vault の審議フォルダへ
    prompts_path = None
    try:
        vault = Path(getattr(cfg, "INVESTMENT_DEPT_VAULT_DIR", ""))
        if vault and vault.exists():
            d = vault / "20_審議" / case_id
            d.mkdir(parents=True, exist_ok=True)
            pp = d / "_起動プロンプト.md"
            pp.write_text(prompts_md, encoding="utf-8")
            prompts_path = str(pp)
            print(f"  ✅ 起動プロンプト生成: {pp}")
    except Exception as e:
        print(f"  ⚠️ プロンプト書込エラー: {str(e)[:60]}")

    return {"ticker": ticker, "case_id": case_id, "packet": packet_path,
            "prompts_path": prompts_path, "skipped": False}


def main():
    ap = argparse.ArgumentParser(description="B部門隔週自動審議の準備（E3）")
    ap.add_argument("--ticker", default="", help="対象銘柄（省略時は自動選定）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    try:
        import config as cfg
    except Exception:
        cfg = None
    run_prep(cfg, args.ticker, args.dry_run)


if __name__ == "__main__":
    main()
